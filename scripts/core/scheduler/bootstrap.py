"""Windows-started Creation Assistant runtime bootstrap.

This process only loads the internal schedule and executor declarations.  It
does not contain a business time, a domain, an agent name, or a model choice.
Core remains the caller that decides whether a business operation is due.
"""

from __future__ import annotations

import argparse
import json
import threading

from scripts.core.external_adapters.windows_process import single_instance_guard
from .runtime import CreationAssistantScheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the Creation Assistant runtime")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="keep the runtime process alive for the system startup entry",
    )
    args = parser.parse_args(argv)
    scheduler = CreationAssistantScheduler()
    status = json.dumps(scheduler.status(), ensure_ascii=False, sort_keys=True)
    if not args.serve:
        print(status, flush=True)
        return 0
    with single_instance_guard("CreationAssistant_Scheduler"):
        threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
