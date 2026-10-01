"""Small stdlib HTTP API and single-page UI for the Stage 4 MVP."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from assaypilot.stage4_service import Stage4RunService, Stage4ServiceError


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
MAX_BODY_BYTES = 16 * 1024
_RUN_ID = re.compile(r"^stage4-[0-9a-f]{32}$")


class Stage4HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: Stage4RunService):
        self.service = service
        super().__init__(address, handler)


class Stage4Handler(BaseHTTPRequestHandler):
    server: Stage4HTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Request paths and identifiers are kept out of shared process logs.
        return

    def _send(self, status: int, body: bytes, content_type: str, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, error: Stage4ServiceError) -> None:
        self._json(error.http_status, {
            "error": {"code": error.code, "message": error.message},
        })

    def _body(self) -> object:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdecimal():
            raise Stage4ServiceError("invalid_request", "Content-Length가 필요합니다.", 411)
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            raise Stage4ServiceError("request_too_large", "요청 본문이 너무 큽니다.", 413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Stage4ServiceError("invalid_request", "JSON 요청 본문을 읽을 수 없습니다.") from exc

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/assets/app.js":
            return self._file("app.js", "text/javascript; charset=utf-8")
        if path == "/assets/app.css":
            return self._file("app.css", "text/css; charset=utf-8")
        if path == "/api/readiness":
            try:
                return self._json(200, self.server.service.readiness())
            except Exception:
                return self._json(503, {"ready": False, "error": {"code": "readiness_failed", "message": "실행 환경을 확인하지 못했습니다."}})
        if path == "/api/runs":
            try:
                return self._json(200, {"runs": self.server.service.recent_runs()})
            except Exception:
                return self._json(500, {"error": {"code": "history_failed", "message": "실행 이력을 읽지 못했습니다."}})
        match = re.fullmatch(r"/api/runs/(stage4-[0-9a-f]{32})(/download)?", path)
        if match:
            run_id = match.group(1)
            try:
                if match.group(2):
                    payload = self.server.service.download(run_id)
                    return self._send(200, payload, "application/json; charset=utf-8", headers={
                        "Content-Disposition": f'attachment; filename="{run_id}-public-results.json"',
                    })
                return self._json(200, self.server.service.get_run(run_id))
            except Stage4ServiceError as exc:
                return self._error(exc)
            except Exception:
                return self._json(500, {"error": {"code": "run_read_failed", "message": "공개 실행 상태를 읽지 못했습니다."}})
        return self._json(404, {"error": {"code": "not_found", "message": "요청한 자원을 찾을 수 없습니다."}})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/runs":
                value = self._body()
                result, reused = self.server.service.start_run(value, self.headers.get("Idempotency-Key", ""))
                return self._json(200 if reused else 202, {"run": result, "idempotent_replay": reused})
            match = re.fullmatch(r"/api/runs/(stage4-[0-9a-f]{32})/resume", path)
            if match:
                return self._json(202, {"run": self.server.service.resume_run(match.group(1))})
            return self._json(404, {"error": {"code": "not_found", "message": "요청한 자원을 찾을 수 없습니다."}})
        except Stage4ServiceError as exc:
            return self._error(exc)
        except Exception:
            return self._json(500, {"error": {"code": "service_error", "message": "요청을 처리하지 못했습니다."}})

    def _file(self, name: str, content_type: str) -> None:
        try:
            body = (WEB_ROOT / name).read_bytes()
        except OSError:
            return self._json(404, {"error": {"code": "not_found", "message": "화면 파일을 찾을 수 없습니다."}})
        return self._send(200, body, content_type)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assaypilot-stage4-web")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; default is localhost only")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-root", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if args.host == "localhost":
        loopback_only = True
    else:
        try:
            address = ipaddress.ip_address(args.host)
            loopback_only = address.version == 4 and address.is_loopback
        except ValueError:
            loopback_only = False
    if not loopback_only:
        raise SystemExit("Stage 4 MVP accepts loopback addresses only; use an authenticated deployment proxy for remote access")
    service = Stage4RunService(args.runtime_root) if args.runtime_root else Stage4RunService()
    server = Stage4HTTPServer((args.host, args.port), Stage4Handler, service)
    print(f"AssayPilot Stage 4 listening at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
