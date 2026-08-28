"""Hermes-owned one-shot entry for the production daily operation.

This is deliberately a process-and-exit carrier.  Hermes owns the schedule
and invokes this entry; the project core owns the business execution and
formal writes.  No listener, HTTP server, or resident scheduler is started.
"""

from __future__ import annotations

from scripts.agent_platform.run_daily_collection_once import main as run_daily_once


if __name__ == "__main__":
    raise SystemExit(run_daily_once(["--data-identity", "production"]))
