#!/usr/bin/env python3
"""Fail if a workflow here targets a runner that hands jobs a Docker socket.

docker-build is for ai-base.yaml only, and ai-base.yaml must not run on pull
requests. docker is for app repos, never this one. Labels aren't access
control: this catches mistakes, not a PR that edits both files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

BUILD_LABEL = "docker-build"
BUILD_WORKFLOW = "ai-base.yaml"
APP_LABEL = "docker"
PR_EVENTS = {"pull_request", "pull_request_target"}


def labels(runs_on: object) -> set[str]:
    if isinstance(runs_on, str):
        return {runs_on}
    if isinstance(runs_on, list):
        return {str(x) for x in runs_on}
    if isinstance(runs_on, dict):
        return labels(runs_on.get("labels", []))
    return set()


def events(on: object) -> set[str]:
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(x) for x in on}
    return set()


def check(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text()) or {}
    # YAML 1.1 reads a bare `on:` key as True.
    on = doc.get("on", doc.get(True))
    problems = []
    for name, job in (doc.get("jobs") or {}).items():
        used = labels((job or {}).get("runs-on"))
        where = f"{path}: job {name}"
        if APP_LABEL in used:
            problems.append(f"{where} uses runs-on: {APP_LABEL}, which is for app repos only")
        if BUILD_LABEL in used:
            if path.name != BUILD_WORKFLOW:
                problems.append(f"{where} uses runs-on: {BUILD_LABEL}, which is for {BUILD_WORKFLOW} only")
            elif events(on) & PR_EVENTS:
                problems.append(f"{where} uses runs-on: {BUILD_LABEL} in a workflow that runs on pull requests")
    return problems


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".gitea/workflows")
    files = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
    if not files:
        print(f"no workflows found in {root}", file=sys.stderr)
        return 1
    problems = [p for f in files for p in check(f)]
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"runner labels OK in {len(files)} workflows")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
