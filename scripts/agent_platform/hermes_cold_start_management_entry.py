"""Reject the retired formal entry before accessing business data."""

from __future__ import annotations


RETIREMENT_MESSAGE = 'the legacy Hermes cold-start management route is retired; use the Creation Assistant Core formal entry'


def main() -> int:
    raise SystemExit(RETIREMENT_MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main())
