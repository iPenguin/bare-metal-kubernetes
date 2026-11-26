#!/usr/bin/env python3
"""Fail if renovate.json is invalid JSON or lowers minimumReleaseAge below 48 hours."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MIN_HOURS = 48
AGE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|h|days?|d|minutes?|mins?|m|weeks?|w)?$",
    re.I,
)


def parse_age(value: object) -> float:
    if isinstance(value, (int, float)):
        # Renovate accepts a number as days.
        return float(value) * 24
    text = str(value).strip()
    match = AGE.fullmatch(text)
    if not match:
        raise ValueError(f"unrecognized minimumReleaseAge: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "days").lower()
    if unit.startswith("w"):
        return amount * 24 * 7
    if unit.startswith("d"):
        return amount * 24
    if unit.startswith("h"):
        return amount
    if unit.startswith("m"):
        return amount / 60
    raise ValueError(f"unrecognized minimumReleaseAge unit in {value!r}")


def check(obj: object, path: str, problems: list[str]) -> None:
    if isinstance(obj, dict):
        if "minimumReleaseAge" in obj:
            hours = parse_age(obj["minimumReleaseAge"])
            if hours < MIN_HOURS:
                problems.append(
                    f"{path}: minimumReleaseAge {obj['minimumReleaseAge']!r} "
                    f"is under {MIN_HOURS} hours"
                )
        for key, value in obj.items():
            check(value, f"{path}.{key}", problems)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            check(value, f"{path}[{index}]", problems)


def main() -> int:
    path = Path("renovate.json")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"renovate.json: invalid JSON: {exc}", file=sys.stderr)
        return 1
    problems: list[str] = []
    try:
        check(data, "renovate.json", problems)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("renovate.json: ok (minimumReleaseAge not lowered)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
