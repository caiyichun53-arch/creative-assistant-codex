"""Reject the retired formal entry before accessing business data."""

from __future__ import annotations

from typing import Any


RETIREMENT_MESSAGE = 'the legacy Hermes formal research route is retired; use the Creation Assistant Core formal entry'


def run(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError(RETIREMENT_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    raise SystemExit(RETIREMENT_MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main())
