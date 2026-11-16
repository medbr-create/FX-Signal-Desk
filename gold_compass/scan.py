"""Analyze locally supplied historical/live-provided spot OHLC JSON, with NO network access.

Run: python -m gold_compass.scan path/to/ohlc.json
Output: JSON that is NOT a trade instruction.
"""
import json
import sys
from pathlib import Path

from .engine import analyze


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m gold_compass.scan OHLC.json", file=sys.stderr)
        return 2
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = analyze(payload)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(json.dumps({"state": "NO_DATA", "reason": f"Invalid input: {exc}"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
