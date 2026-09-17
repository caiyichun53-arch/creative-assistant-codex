"""Reject the retired formal entry before accessing business data."""

from __future__ import annotations


RETIREMENT_MESSAGE = 'the legacy direct formal daily entry is retired; use the Creation Assistant Core scheduler'


def main(argv: list[str] | None = None) -> int:
    raise SystemExit(RETIREMENT_MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main())
