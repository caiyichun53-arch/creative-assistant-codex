"""Serve the Core unified status through a small local read-only Web page."""

from __future__ import annotations

import argparse
import json
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.core.core_entry import build_status


STATIC_ROOT = Path(__file__).resolve().parent / "static"
IDENTITIES = {"production", "test"}


class ReadOnlyWebApplication:
    """Bind one local Web process to one explicit Core data identity."""

    def __init__(self, data_identity: str = "production", database_path: Path | str | None = None) -> None:
        identity = str(data_identity or "").strip().lower()
        if identity not in IDENTITIES:
            raise ValueError("data identity must be explicitly 'production' or 'test'")
        self.data_identity = identity
        self.database_path = Path(database_path) if database_path is not None else None

    def read_status(self) -> dict[str, Any]:
        """Read the authoritative status from Core and nowhere else."""

        return build_status(
            data_identity=self.data_identity,
            database_path=self.database_path,
        )


class ReadOnlyRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler with only status and static-asset GET routes."""

    server_version = "CreationAssistantReadOnlyWeb/1.0"

    def __init__(self, *args: Any, application: ReadOnlyWebApplication, **kwargs: Any) -> None:
        self.application = application
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._serve_status()
            return
        if path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "\u672c\u5730\u9875\u9762\u53ea\u5141\u8bb8\u8bfb\u53d6"}, body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _serve_status(self) -> None:
        try:
            status = self.application.read_status()
        except Exception as exc:  # Core/database/config read failures are shown, never replaced.
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "source": "Creation Assistant Core",
                    "error": f"\u72b6\u6001\u8bfb\u53d6\u5931\u8d25\uff1a{exc}",
                },
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "status": status})

    def _serve_static(self, relative_name: str) -> None:
        try:
            candidate = (STATIC_ROOT / relative_name).resolve()
            candidate.relative_to(STATIC_ROOT.resolve())
        except (OSError, ValueError):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})
            return
        if not candidate.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(candidate.suffix, "application/octet-stream")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": f"\u9875\u9762\u8bfb\u53d6\u5931\u8d25\uff1a{exc}"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        payload = json.dumps({"ok": False, "error": "\u672c\u5730\u9875\u9762\u53ea\u5141\u8bb8\u8bfb\u53d6"}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, body: bool = True) -> None:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if body:
            self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the standard local request log on stderr only."""

        super().log_message(format, *args)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    data_identity: str = "production",
    database_path: Path | str | None = None,
) -> ThreadingHTTPServer:
    """Create a local server; the caller owns its lifecycle."""

    application = ReadOnlyWebApplication(data_identity=data_identity, database_path=database_path)
    handler = partial(ReadOnlyRequestHandler, application=application)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.application = application  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Creation Assistant local read-only Web status")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-identity", choices=("production", "test"), default="production")
    parser.add_argument("--database-path", type=Path)
    args = parser.parse_args(argv)
    server = create_server(
        args.host,
        args.port,
        data_identity=args.data_identity,
        database_path=args.database_path,
    )
    print(f"Creation Assistant read-only Web: http://{args.host}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
