#!/usr/bin/env python3
"""Loopback-only web Operator for ordinary crawler batches."""
from __future__ import annotations

import argparse
import email.policy
import html
import ipaddress
import json
import mimetypes
import secrets
import sys
import threading
import time
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from operator_core import BatchManager, OperatorError, OperatorSettings


ROOT = Path(__file__).resolve().parents[1]


class SessionStore:
    def __init__(self, lifetime_seconds: int):
        self.lifetime = max(60, min(int(lifetime_seconds), 3600))
        self._values: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self) -> tuple[str, str]:
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            self._values[session] = (csrf, time.monotonic() + self.lifetime)
        return session, csrf

    def get(self, cookie_header: str | None) -> tuple[str, str] | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            session = cookie["operator_session"].value
        except (KeyError, ValueError):
            return None
        now = time.monotonic()
        with self._lock:
            expired = [key for key, (_, deadline) in self._values.items() if deadline <= now]
            for key in expired:
                self._values.pop(key, None)
            value = self._values.get(session)
        return (session, value[0]) if value else None


def _multipart_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise OperatorError("multipart_required")
    try:
        encoded_type = content_type.encode("ascii", "strict")
    except UnicodeError:
        raise OperatorError("multipart_required") from None
    message = BytesParser(policy=email.policy.default).parsebytes(
        b"Content-Type: " + encoded_type + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    files = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data" or part.get_param("name", header="content-disposition") != "file":
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if filename is not None and isinstance(payload, bytes):
            files.append((filename, payload))
    if len(files) != 1:
        raise OperatorError("one_xlsx_file_required")
    return files[0]


def create_server(manager: BatchManager, *, host: str = "127.0.0.1", port: int = 8772) -> ThreadingHTTPServer:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Operator must bind to loopback")
    except ValueError:
        if host != "localhost":
            raise ValueError("Operator must bind to loopback") from None
    sessions = SessionStore(manager.settings.session_seconds)
    static_root = manager.settings.release_root / "operator"

    class OperatorHandler(BaseHTTPRequestHandler):
        server_version = "AmazonOperator/1"

        def log_message(self, _format, *_args):
            return

        def _headers(self, status: int, content_type: str, length: int, *, disposition: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if disposition:
                self.send_header("Content-Disposition", disposition)

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, error: OperatorError):
            self._json(error.status, {"error": {"code": error.code}})

        def _loopback_request(self) -> bool:
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def _host_allowed(self) -> bool:
            raw = str(self.headers.get("Host") or "")
            host = raw.rsplit(":", 1)[0].strip("[]").lower()
            return host in {"127.0.0.1", "localhost", "::1"}

        def _session(self, *, csrf: bool = False) -> tuple[str, str]:
            value = sessions.get(self.headers.get("Cookie"))
            if value is None:
                raise OperatorError("unauthorized", 401)
            if csrf and not secrets.compare_digest(str(self.headers.get("X-CSRF-Token") or ""), value[1]):
                raise OperatorError("csrf_rejected", 403)
            return value

        def _body(self, limit: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise OperatorError("invalid_content_length", 411) from None
            if length < 0 or length > limit:
                raise OperatorError("request_too_large", 413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise OperatorError("incomplete_request")
            return body

        def _discard_auth_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            maximum = manager.settings.xlsx_limits.max_file_bytes + 1024 * 1024
            if 0 < length <= maximum:
                self.rfile.read(length)
            self.close_connection = True

        def _json_body(self) -> dict:
            body = self._body(64 * 1024)
            try:
                value = json.loads(body)
            except (TypeError, ValueError):
                raise OperatorError("invalid_json") from None
            if not isinstance(value, dict):
                raise OperatorError("invalid_json")
            return value

        def _guard(self):
            if not self._loopback_request() or not self._host_allowed():
                raise OperatorError("loopback_only", 403)

        def do_GET(self):
            try:
                self._guard()
                parsed = urlsplit(self.path)
                if parsed.path == "/":
                    session, csrf = sessions.issue()
                    try:
                        template = (static_root / "index.html").read_text(encoding="utf-8")
                    except OSError:
                        template = '<!doctype html><html><body data-csrf="__CSRF__"><h1>普通爬虫</h1><script src="/app.js"></script></body></html>'
                    body = template.replace("__CSRF__", csrf).replace(
                        "__CONSOLE_URL__", html.escape(manager.dashboard_url(), quote=True)
                    ).replace(
                        "__BUSINESS_TENANT__", html.escape(manager.settings.business_tenant_id)
                    ).encode("utf-8")
                    self._headers(200, "text/html; charset=utf-8", len(body))
                    self.send_header("Set-Cookie", f"operator_session={session}; Path=/; Max-Age={sessions.lifetime}; HttpOnly; SameSite=Strict")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path in {"/app.js", "/styles.css"}:
                    filename = parsed.path.removeprefix("/")
                    try:
                        body = (static_root / filename).read_bytes()
                    except OSError:
                        raise OperatorError("not_found", 404) from None
                    content_type = "application/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
                    self._headers(200, content_type, len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/healthz":
                    self._json(200, {"ok": True, "service": "ordinary-crawler-operator"})
                    return
                self._session()
                query = parse_qs(parsed.query)
                if parsed.path == "/api/get_batch":
                    batch_id = str((query.get("batch_id") or [""])[0]).strip() or None
                    self._json(200, manager.get_batch(batch_id))
                    return
                if parsed.path == "/api/download_artifact":
                    batch_id = str((query.get("batch_id") or [""])[0])
                    artifact = str((query.get("artifact") or [""])[0])
                    path, content_type, filename = manager.download_artifact(batch_id, artifact)
                    body = path.read_bytes()
                    self._headers(200, content_type, len(body), disposition=f'attachment; filename="{filename}"')
                    self.end_headers()
                    self.wfile.write(body)
                    return
                raise OperatorError("not_found", 404)
            except OperatorError as exc:
                self._error(exc)
            except Exception:
                self._error(OperatorError("internal_error", 500))

        def do_POST(self):
            try:
                self._guard()
                self._session(csrf=True)
                parsed = urlsplit(self.path)
                if parsed.path == "/api/upload_preview":
                    content_type = str(self.headers.get("Content-Type") or "")
                    if content_type.lower().startswith("application/json"):
                        value = self._json_body()
                        if set(value) - {"import_id", "selected_sheet", "selected_column"}:
                            raise OperatorError("invalid_request")
                        result = manager.upload_preview(
                            "", b"", import_id=str(value.get("import_id") or ""),
                            selected_sheet=str(value.get("selected_sheet") or ""),
                            selected_column=str(value.get("selected_column") or "").upper(),
                        )
                    else:
                        body = self._body(manager.settings.xlsx_limits.max_file_bytes + 1024 * 1024)
                        filename, payload = _multipart_file(content_type, body)
                        result = manager.upload_preview(filename, payload)
                    self._json(200, result)
                    return
                value = self._json_body()
                if parsed.path == "/api/start_batch":
                    if set(value) - {"import_id", "worker_count", "subject_type"}:
                        raise OperatorError("invalid_request")
                    result = manager.start_batch(
                        str(value.get("import_id") or ""),
                        int(value.get("worker_count") or 1),
                        str(value.get("subject_type") or "own"),
                    )
                    self._json(202, result)
                    return
                if parsed.path == "/api/stop_batch":
                    if set(value) != {"batch_id"}:
                        raise OperatorError("invalid_request")
                    self._json(200, manager.stop_batch(str(value["batch_id"])))
                    return
                raise OperatorError("not_found", 404)
            except OperatorError as exc:
                if exc.status in {401, 403}:
                    self._discard_auth_body()
                self._error(exc)
            except (TypeError, ValueError):
                self._error(OperatorError("invalid_request"))
            except Exception:
                self._error(OperatorError("internal_error", 500))

    return ThreadingHTTPServer((host, int(port)), OperatorHandler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--agent-port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data" / "operator")
    parser.add_argument("--config-path", type=Path, default=ROOT / "config" / "amazon_us.windows.toml")
    parser.add_argument("--vault-path", type=Path, default=ROOT / "data" / "secure" / "amazon_us.secrets.dpapi")
    parser.add_argument("--internal-console-port", type=int, default=8773)
    parser.add_argument("--dashboard-port", type=int, default=8770)
    parser.add_argument("--business-tenant-id", default="amazon_us_main")
    parser.add_argument("--campaign-deadline-seconds", type=int, default=86400)
    parser.add_argument("--worker-python", type=Path)
    parser.add_argument("--fake-runner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fake-delay-seconds", type=float, default=0.05, help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = OperatorSettings(
        release_root=ROOT,
        data_root=args.data_root,
        config_path=args.config_path,
        vault_path=args.vault_path,
        port=args.port,
        agent_port=args.agent_port,
        internal_console_port=args.internal_console_port,
        dashboard_port=args.dashboard_port,
        business_tenant_id=args.business_tenant_id,
        campaign_deadline_seconds=args.campaign_deadline_seconds,
        python_executable=args.worker_python,
        fake_runner=args.fake_runner,
        fake_delay_seconds=args.fake_delay_seconds,
    )
    server = create_server(BatchManager(settings), host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
