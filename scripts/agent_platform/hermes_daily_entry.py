"""Retired Hermes-named formal daily entry.

The old Hermes process-and-exit route is retained as historical material, but
it no longer forwards into the formal daily operation. Formal daily work can
only be started through the Creation Assistant Core scheduler.
"""

from __future__ import annotations

RETIREMENT_MESSAGE = (
    "the legacy Hermes formal daily route is retired; "
    "use the Creation Assistant Core scheduler"
)


def main() -> int:
    raise SystemExit(RETIREMENT_MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main())
