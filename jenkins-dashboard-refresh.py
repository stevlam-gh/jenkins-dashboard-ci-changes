#!/usr/bin/env python3
"""Serve the local CI experiment dashboard and fetch Jenkins timing data.

The server binds only to localhost. The dashboard can submit Jenkins
credentials for a single refresh; credentials are never returned or logged.
Environment variables and /tmp/tok remain available as a CLI fallback.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "jenkins-ci-dashboard.html"
BASE_URL = "https://jenkins-tejas.cdaas.umbrella.com"
JOB_PATH = "job/PR-build-for-monorepo/job"
BRANCHES = {
    "main": "Baseline",
    "fb-ci-changes": "sccache + Artifactory experiment",
    "fb-file-write-protection": "File-write protection experiment",
}
STAGES = [
    "CI Pipeline",
    "Build",
    "Compile and Link - linux",
    "Compile and Link - mac",
    "Compile and Link - windows",
    "CMake Configure - linux",
    "CMake Configure - mac",
    "CMake Configure - windows",
    "Conan Setup - linux",
    "Conan Setup - mac",
    "Conan Setup - windows",
    "Dev Build - Alma9",
    "Dev Build - Mac26arm",
    "Dev Build - WindowsServer2025",
    "Sonar Coverage Scope",
    "Sonar Coverage",
    "Sonar Publish",
    "Sonar Analysis",
]
SONAR_MARKER = re.compile(r"\[ci\]\[sonar\]|sonar-scanner|Executing normal Sonar analysis path|Active Sonar platforms|Sonar Analysis", re.IGNORECASE)
SONAR_STAGE = re.compile(r"^Sonar(?:\s|$)", re.IGNORECASE)
DEV_BUILD_STAGE = re.compile(r"^Dev Build\s+-\s+", re.IGNORECASE)
WAIT_STAGE = re.compile(
    r"^(?:Controller Queue|Node Executor Wait|Node Allocation|CTF Capacity Wait)",
    re.IGNORECASE,
)


def culprit_label(stage, clock):
    lower = str(stage or "").lower()
    if "node executor wait" in lower or "ctf capacity wait" in lower:
        return "Executor/capacity wait"
    if "node allocation" in lower or clock == "scheduled-to-agent":
        return "Executor/node provisioning"
    if lower.startswith("sonar"):
        return f"Sonar: {stage}"
    if lower.startswith("dev build"):
        return f"Build: {str(stage).replace('Dev Build - ', '', 1)}"
    if "test" in lower:
        return f"Tests: {stage}"
    return stage or "Unknown"


def metric_stat(values):
    values = sorted(value for value in values if isinstance(value, (int, float)))
    if not values:
        return {"n": 0, "min": None, "avg": None, "median": None, "max": None}
    return {
        "n": len(values),
        "min": values[0],
        "avg": sum(values) / len(values),
        "median": values[(len(values) - 1) // 2],
        "max": values[-1],
    }


def read_credentials(credentials=None):
    credentials = credentials or {}
    username = str(credentials.get("username") or os.environ.get("JENKINS_USER") or "edlp_auto.gen").strip()
    token = str(credentials.get("apiKey") or os.environ.get("JENKINS_API_TOKEN") or "").strip()
    if not token:
        token_path = Path("/tmp/tok")
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
    if not username:
        raise RuntimeError("Jenkins username is required")
    if not token:
        raise RuntimeError("Jenkins API key is required")
    return username, token


def client(username, token):
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {credentials}", "User-Agent": "ci-experiment-dashboard"}


def request(url, headers, accept_json=False):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Jenkins request failed with HTTP {error.code}: {url.split('?')[0]}") from error
    return json.loads(body) if accept_json else body.decode("utf-8", errors="replace")


def branch_url(branch, suffix=""):
    return f"{BASE_URL}/{JOB_PATH}/{urllib.parse.quote(branch, safe='')}{suffix}"


def timing_events(text):
    events = []
    marker = "[ci-timing] "
    for line in text.splitlines():
        if marker not in line:
            continue
        try:
            events.append(json.loads(line.split(marker, 1)[1]))
        except json.JSONDecodeError:
            continue
    return events


def cache_evidence(text):
    rates = [float(value) for value in re.findall(r"Cache hits rate\s+([0-9.]+) %", text, flags=re.I)]
    if not rates:
        return None
    maximum = max(rates)
    return {
        "rates": rates,
        "maxHitRate": maximum,
        "minHitRate": min(rates),
        "status": "warm" if maximum >= 99.99 else "cold" if maximum == 0 else "mixed",
    }


def interval_union_minutes(events):
    intervals = sorted(
        (event.get("startedAtMillis"), event.get("finishedAtMillis"))
        for event in events
        if isinstance(event.get("startedAtMillis"), (int, float))
        and isinstance(event.get("finishedAtMillis"), (int, float))
        and event.get("finishedAtMillis") >= event.get("startedAtMillis")
    )
    merged = []
    for start, finish in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, finish])
        else:
            merged[-1][1] = max(merged[-1][1], finish)
    return sum(finish - start for start, finish in merged) / 60000


def wait_intervals(events, build_number):
    return [
        event
        for event in events
        if str(event.get("buildNumber")) == str(build_number)
        and event.get("eventType") == "complete"
        and WAIT_STAGE.search(str(event.get("stage", "")))
    ]


def overlap_minutes(event, waits):
    start = event.get("startedAtMillis")
    finish = event.get("finishedAtMillis")
    if not isinstance(start, (int, float)) or not isinstance(finish, (int, float)):
        return 0.0
    intervals = []
    for wait in waits:
        wait_start = wait.get("startedAtMillis")
        wait_finish = wait.get("finishedAtMillis")
        if not isinstance(wait_start, (int, float)) or not isinstance(wait_finish, (int, float)):
            continue
        clipped_start = max(start, wait_start)
        clipped_finish = min(finish, wait_finish)
        if clipped_finish >= clipped_start:
            intervals.append((clipped_start, clipped_finish))
    return interval_union_minutes(
        [{"startedAtMillis": start, "finishedAtMillis": finish} for start, finish in intervals]
    )


def retry_evidence(events, builds, manual_reruns):
    """Summarize automatic Build-stage retries visible in [ci-timing] events."""
    by_build = {}
    for event in events:
        if event.get("eventType") != "complete" or event.get("stage") != "Build":
            continue
        number = str(event.get("buildNumber"))
        by_build.setdefault(number, []).append(event)

    details = []
    for build in builds:
        records = by_build.get(str(build["number"]), [])
        if not records:
            continue
        attempts = sorted({int(record.get("attempt") or 1) for record in records})
        statuses = sorted({str(record.get("status") or "") for record in records})
        failure_classes = sorted({str(record.get("failureClass") or "") for record in records if record.get("failureClass")})
        details.append({
            "build": build["number"],
            "attempts": len(attempts),
            "retries": max(0, len(attempts) - 1),
            "statuses": statuses,
            "failureClasses": failure_classes,
            "successfulAfterRetry": len(attempts) > 1 and "SUCCESS" in statuses,
        })

    retry_builds = [detail for detail in details if detail["retries"] > 0]
    return {
        "source": "Build-stage [ci-timing] events",
        "instrumentedBuilds": len(details),
        "retryBuilds": len(retry_builds),
        "extraRetries": sum(detail["retries"] for detail in details),
        "successfulAfterRetry": sum(1 for detail in retry_builds if detail["successfulAfterRetry"]),
        "timeoutRetryBuilds": sum(1 for detail in retry_builds if "TIMEOUT" in detail["failureClasses"]),
        "manualReruns": manual_reruns,
        "builds": details,
    }


def refresh(limit, output_dir, credentials=None):
    headers = client(*read_credentials(credentials))
    output_dir.mkdir(parents=True, exist_ok=True)
    branches = {}
    tree = urllib.parse.quote(f"builds[number,result,duration,timestamp]{{0,{limit}}}", safe="[],{}")

    for branch, label in BRANCHES.items():
        branch_dir = output_dir / branch
        branch_dir.mkdir(parents=True, exist_ok=True)
        builds_payload = request(branch_url(branch, f"/api/json?tree={tree}"), headers, accept_json=True)
        (branch_dir / "builds.json").write_text(json.dumps(builds_payload, indent=2), encoding="utf-8")
        builds = [
            {
                "number": build["number"],
                "result": build["result"],
                "durationMin": build["duration"] / 60000,
                "timestamp": build.get("timestamp"),
            }
            for build in builds_payload.get("builds", [])
            if build.get("result") is not None and build.get("duration", 0) > 0
        ]
        events = []
        sonar_runs = []
        cache = {}
        manual_reruns = []
        for build in builds:
            text = request(branch_url(branch, f"/{build['number']}/consoleText"), headers)
            (branch_dir / f"{build['number']}.log").write_text(text, encoding="utf-8")
            events.extend(timing_events(text))
            for match in re.finditer(r"(?im)^\s*(Rebuilds build|Replayed)\s+#?(\d+)", text):
                manual_reruns.append({
                    "build": build["number"],
                    "type": "rebuild" if match.group(1).lower().startswith("rebuild") else "replay",
                    "parentBuild": int(match.group(2)),
                })
            if SONAR_MARKER.search(text):
                sonar_runs.append(build["number"])
                build["sonar"] = True
            evidence = cache_evidence(text)
            if evidence:
                cache[str(build["number"])] = evidence
                build["cache"] = evidence["status"]
                build["cacheRates"] = evidence["rates"]

        # Parent Sonar timing events represent elapsed wall time for the stage.
        # Do not add the per-platform child events as well, or the parallel work
        # would be counted multiple times. Builds without an explicit parent
        # timing event remain unchanged when the dashboard subtracts Sonar time.
        for build in builds:
            sonar_minutes = sum(
                event.get("elapsedMs", 0) / 60000
                for event in events
                if str(event.get("buildNumber")) == str(build["number"])
                and event.get("eventType") == "complete"
                and SONAR_STAGE.search(str(event.get("stage", "")))
                and not event.get("platform")
            )
            if sonar_minutes > 0:
                build["sonarDurationMin"] = sonar_minutes

            platform_minutes = {"linux": 0.0, "mac": 0.0, "windows": 0.0}
            for event in events:
                if str(event.get("buildNumber")) != str(build["number"]):
                    continue
                if event.get("eventType") != "complete" or not DEV_BUILD_STAGE.search(str(event.get("stage", ""))):
                    continue
                platform = str(event.get("platform", "")).lower()
                key = "mac" if "mac" in platform else "windows" if "windows" in platform else "linux"
                platform_minutes[key] += event.get("elapsedMs", 0) / 60000
            if any(platform_minutes.values()):
                build["platformRuntimeMin"] = {key: value for key, value in platform_minutes.items() if value > 0}

            waits = wait_intervals(events, build["number"])
            wait_minutes = interval_union_minutes(waits)
            if wait_minutes > 0:
                build["waitMinutes"] = wait_minutes

            pipeline_values = []
            for event in events:
                if (
                    str(event.get("buildNumber")) == str(build["number"])
                    and event.get("eventType") == "complete"
                    and event.get("stage") == "CI Pipeline"
                ):
                    pipeline_values.append(max(0, event.get("elapsedMs", 0) / 60000 - overlap_minutes(event, waits)))
            if pipeline_values:
                build["pipelineValues"] = pipeline_values

            culprit_events = [
                event for event in events
                if str(event.get("buildNumber")) == str(build["number"])
                and event.get("eventType") == "complete"
                and event.get("stage") not in ("CI Pipeline", "Build", "Post-Agent Wall Clock")
            ]
            if culprit_events:
                culprit = max(culprit_events, key=lambda event: event.get("elapsedMs", 0))
                build["culprit"] = {
                    "label": culprit_label(culprit.get("stage"), culprit.get("clock")),
                    "minutes": culprit.get("elapsedMs", 0) / 60000,
                }

        outcomes = {}
        for build in builds:
            outcomes[build["result"]] = outcomes.get(build["result"], 0) + 1
        failed = sum(outcomes.get(result, 0) for result in ("FAILURE", "ABORTED", "NOT_BUILT", "UNSTABLE"))
        stage_metrics = {}
        for stage in STAGES:
            values = [event["elapsedMs"] / 60000 for event in events if event.get("eventType") == "complete" and event.get("stage") == stage]
            successful = [event["elapsedMs"] / 60000 for event in events if event.get("eventType") == "complete" and event.get("stage") == stage and event.get("status") == "SUCCESS"]
            stage_metrics[stage] = {"all": metric_stat(values), "success": metric_stat(successful)}
        branches[branch] = {
            "label": label,
            "builds": builds,
            "outcomes": outcomes,
            "failureRate": failed / len(builds) * 100 if builds else None,
            "duration": {
                "all": metric_stat([build["durationMin"] for build in builds]),
                "success": metric_stat([build["durationMin"] for build in builds if build["result"] == "SUCCESS"]),
            },
            "stageMetrics": stage_metrics,
            "sonarRuns": sorted(set(sonar_runs)),
            "cacheRuns": [{"build": int(number), **value} for number, value in sorted(cache.items(), key=lambda item: int(item[0]))],
            "retryEvidence": retry_evidence(events, builds, manual_reruns),
            "worstRuns": sorted(builds, key=lambda build: build["durationMin"], reverse=True)[:8],
        }

    return {
        "generatedAt": datetime.now(timezone.utc).date().isoformat(),
        "source": "Jenkins API duration metadata and full consoleText logs (local refresh)",
        "branches": branches,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CIExperimentDashboard/1.0"

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _refresh_response(self, credentials=None, query=None):
        try:
            query = query or {}
            limit = max(1, min(int(query.get("limit", [50])[0]), 200))
            payload = refresh(limit, self.server.output_dir, credentials)
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:  # keep credentials out of the response
            body = str(error).encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/api/refresh":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._refresh_response(query=query)
            return
        if self.path.split("?", 1)[0] == "/":
            body = DASHBOARD.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/refresh":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Refresh request must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            payload = str(error).encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._refresh_response(credentials=body, query={"limit": [body.get("limit", 50)]})

    def log_message(self, format, *args):
        if "/api/refresh" not in format:
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--limit", type=int, default=50, help="maximum completed builds per branch")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "jenkins-full")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.output_dir = args.output_dir
    print(f"Dashboard: http://127.0.0.1:{args.port}/")
    print(f"Logs: {args.output_dir}")
    print("Enter Jenkins credentials in the dashboard and click 'Refresh from Jenkins'; stop with Ctrl-C.")
    server.serve_forever()


if __name__ == "__main__":
    main()
