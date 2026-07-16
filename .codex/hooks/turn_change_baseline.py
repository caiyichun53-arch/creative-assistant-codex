#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import current_stage_id, hook_system_message, modified_files, project_root, read_stdin_json, save_turn_state, turn_id_from_input, utc_now, workspace_fingerprint


def main() -> int:
    try:
        data = read_stdin_json()
        root = project_root()
        state = {
            "turn_id": turn_id_from_input(data),
            "project_root": str(root),
            "stage_id": current_stage_id(root),
            "workspace_fingerprint": workspace_fingerprint(root),
            "modified_files": modified_files(root),
            "recorded_at": utc_now(),
        }
        save_turn_state(root, state)
        return 0
    except Exception as exc:
        hook_system_message(f"turn baseline hook failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
