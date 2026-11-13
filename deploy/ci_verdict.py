"""Read GitHub's check-runs JSON for a commit on stdin and print green, wait or red.

update.sh deploys only on green. Anything unexpected (no answer, rate limit, no checks yet)
prints wait, so the Pi keeps the code it has rather than installing an unchecked commit.
"""

import json
import sys

PASSING = {"success", "skipped", "neutral"}


def verdict(raw: str) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return "wait"
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list) or not runs:
        return "wait"
    if any(run.get("status") != "completed" for run in runs):
        return "wait"
    if all(run.get("conclusion") in PASSING for run in runs):
        return "green"
    return "red"


if __name__ == "__main__":
    print(verdict(sys.stdin.read()))
