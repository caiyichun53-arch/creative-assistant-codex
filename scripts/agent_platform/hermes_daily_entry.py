"""Legacy one-shot carrier for the production daily operation.

This remains a process-and-exit compatibility carrier.  It does not own the
business schedule: the Creation Assistant schedule registry and Core do.
During migration protection the automatic trigger is rejected before formal
storage is opened.  No listener, HTTP server, or resident scheduler starts.
"""

from __future__ import annotations

from scripts.agent_platform.run_daily_collection_once import main as run_daily_once


if __name__ == "__main__":
    raise SystemExit(run_daily_once(["--data-identity", "production"]))
