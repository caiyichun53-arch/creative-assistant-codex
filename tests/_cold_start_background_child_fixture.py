"""Child writer used to prove that stop closes the whole executor tree."""

from __future__ import annotations

import argparse
from pathlib import Path
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, required=True)
    arguments = parser.parse_args()
    count = 0
    while True:
        count += 1
        arguments.marker.write_text(str(count), encoding="utf-8")
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
