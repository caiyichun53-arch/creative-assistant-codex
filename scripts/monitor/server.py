from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scripts.monitor import queries
from scripts.validation.clean_room_empty_db import configured_db_path, ensure_safe_db_path

DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")

STATE_PATH_RE = re.compile(r"^/api/states/([^/]+)$")
JOB_ATTEMPTS_PATH_RE = re.compile(r"^/api/jobs/([^/]+)/attempts$")


def _json_default(value):
    raise TypeError(f"not JSON serializable: {value!r}")


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class MonitorHandler(BaseHTTPRequestHandler):
        server_version = "CreationAssistantMonitor/1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            path = split.path
            params = {k: v[0] for k, v in parse_qs(split.query).items()}

            if path == "/":
                self._send_html(DASHBOARD_HTML_PATH.read_bytes())
                return

            try:
                conn = queries.open_readonly_connection(db_path)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
                return

            try:
                limit = int(params.get("limit", "100"))
                status = params.get("status")

                if path == "/api/summary":
                    self._send_json(queries.summary(conn))
                    return

                if path == "/api/jobs":
                    self._send_json(
                        queries.list_scheduler_jobs(conn, status=status, limit=limit)
                    )
                    return

                match = JOB_ATTEMPTS_PATH_RE.match(path)
                if match:
                    self._send_json(queries.list_job_attempts(conn, match.group(1)))
                    return

                match = STATE_PATH_RE.match(path)
                if match:
                    try:
                        self._send_json(
                            queries.list_state_rows(
                                conn, match.group(1), status=status, limit=limit
                            )
                        )
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, status=404)
                    return

                if path == "/api/audit":
                    self._send_json(
                        queries.list_audit_events(
                            conn, limit=limit, object_kind=params.get("object_kind")
                        )
                    )
                    return

                if path == "/api/outbox":
                    self._send_json(
                        queries.list_outbox_messages(conn, status=status, limit=limit)
                    )
                    return

                if path == "/api/receipts":
                    self._send_json(
                        queries.list_command_receipts(conn, status=status, limit=limit)
                    )
                    return

                if path == "/api/skill-runs":
                    self._send_json(
                        queries.list_skill_runs(conn, status=status, limit=limit)
                    )
                    return

                self._send_json({"error": f"not found: {path}"}, status=404)
            finally:
                conn.close()

    return MonitorHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only local monitoring dashboard for the formal clean-room DB."
    )
    parser.add_argument("--db", help="SQLite DB path. Defaults to config storage.local_validation_db.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default 8787).")
    args = parser.parse_args(argv)

    db_path = ensure_safe_db_path(Path(args.db) if args.db else configured_db_path())
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    handler_cls = make_handler(db_path)
    httpd = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"monitoring dashboard: http://{args.host}:{args.port}/  (db={db_path.as_posix()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
