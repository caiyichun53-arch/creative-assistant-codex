"""Formal runtime liveness policy, without retries or state transitions."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.core.runtime.process_supervision import ManagedProcessTree


ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "config" / "business_guardrails" / "runtime_liveness.json"


@dataclass(frozen=True)
class LivenessBudget:
    first_activity_seconds: float
    stalled_seconds: float


class RuntimeLivenessError(RuntimeError):
    def __init__(self, kind: str, state: str, elapsed_seconds: float, activity_count: int) -> None:
        super().__init__(f"{kind} {state} after {elapsed_seconds:.1f}s with {activity_count} activity event(s)")
        self.kind = kind
        self.state = state
        self.elapsed_seconds = elapsed_seconds
        self.activity_count = activity_count


def budget_for(kind: str) -> LivenessBudget:
    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    values = (payload.get("budgets") or {}).get(kind)
    if not isinstance(values, dict):
        raise RuntimeError(f"runtime liveness budget is missing: {kind}")
    first = float(values.get("first_activity_seconds") or 0)
    stalled = float(values.get("stalled_seconds") or 0)
    if first <= 0 or stalled <= 0:
        raise RuntimeError(f"runtime liveness budget is invalid: {kind}")
    return LivenessBudget(first, stalled)


def run_process_with_liveness(
    command: list[str], *, kind: str, cwd: Path | None = None, env: dict[str, str] | None = None,
    process_options: dict[str, object] | None = None, on_activity: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a local worker only while it emits real stderr/stdout progress.

    Workers keep their formal result on stdout; progress goes to stderr and is
    retained only for the current process result.  No retry is initiated here.
    """
    limits = budget_for(kind)
    child_env = os.environ.copy()
    child_env.update(env or {})
    # Every managed worker must speak UTF-8 to its supervisor.  On Windows the
    # inherited console code page can otherwise make a harmless title emoji
    # abort a material-preparation job before the actual work is finished.
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict[str, object] = {
        "cwd": cwd, "env": child_env, "text": True, "encoding": "utf-8", "errors": "replace",
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False,
    }
    kwargs.update(process_options or {})
    # A collection script can itself start a browser and other children.  Put
    # the whole tree under one OS-owned supervisor so interruption of this
    # Python process also ends those children instead of orphaning them.
    with ManagedProcessTree() as process_tree:
        process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        try:
            process_tree.add(process)
        except Exception:
            process.terminate()
            process.wait()
            raise
        events: queue.Queue[tuple[str, str]] = queue.Queue()
        captured = {"stdout": [], "stderr": []}

        def read_stream(name: str, stream: object) -> None:
            try:
                for line in stream:  # type: ignore[union-attr]
                    captured[name].append(line)
                    events.put((name, line))
            finally:
                events.put((name, ""))

        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            threading.Thread(target=read_stream, args=(name, stream), daemon=True).start()
        started = time.monotonic()
        last_activity = started
        activity_count = 0
        while process.poll() is None:
            now = time.monotonic()
            wait_seconds = limits.first_activity_seconds if activity_count == 0 else limits.stalled_seconds
            remaining = wait_seconds - (now - last_activity)
            if remaining <= 0:
                state = "produced_no_first_activity" if activity_count == 0 else "stalled_after_activity"
                raise RuntimeLivenessError(kind, state, now - started, activity_count)
            try:
                name, line = events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line:
                last_activity = time.monotonic()
                activity_count += 1
                if on_activity is not None:
                    # stdout can be a complete transcript or another formal
                    # result.  It proves liveness but is not progress text and
                    # must not be copied wholesale into task logs.
                    on_activity(
                        "worker produced output" if name == "stdout" else line.strip()
                    )
        process.wait()
        return subprocess.CompletedProcess(command, process.returncode, "".join(captured["stdout"]), "".join(captured["stderr"]))
