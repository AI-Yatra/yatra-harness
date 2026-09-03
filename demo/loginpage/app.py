"""Serve the sign-in page, so a person can look at it.

`python app.py` then http://localhost:8000. Nothing here is part of the
exercise: the flaws are in `auth.py`, `page.py` and `static/style.css`, and
the tests judge those directly. This exists so the difference is something
you can see rather than only something a test reports.

Sessions are a dictionary in memory. Restarting the server signs everyone out,
which is the correct amount of session management for a demonstration.
"""

from __future__ import annotations

import argparse
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import auth
import page

ROOT = Path(__file__).resolve().parent
SESSIONS: dict[str, str] = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "yatra-loginpage/1.0"

    def do_GET(self) -> None:  # noqa: N802 - the name BaseHTTPRequestHandler wants
        if self.path.startswith("/static/"):
            self._serve_static()
            return
        if self.path in ("/", "/login"):
            username = SESSIONS.get(self._session_id())
            if username:
                self._html(page.render_welcome(username))
            else:
                self._html(page.render())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/login":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]

        result = auth.authenticate(username, password)
        if not result.ok:
            # Re-rendered rather than redirected, so the message and the
            # username the visitor typed survive together.
            self._html(page.render(error=result.message, username=username))
            return
        token = secrets.token_urlsafe(16)
        SESSIONS[token] = username.strip().lower()
        body = page.render_welcome(SESSIONS[token]).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- helpers

    def _session_id(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get("session")
        return morsel.value if morsel else ""

    def _serve_static(self) -> None:
        # Resolved and then checked, so `/static/../../etc/passwd` cannot
        # leave the directory. A demonstration is still a web server.
        target = (ROOT / self.path.lstrip("/")).resolve()
        if not target.is_file() or ROOT / "static" not in target.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        kind = "text/css; charset=utf-8" if target.suffix == ".css" else "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, document: str) -> None:
        body = document.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # One tidy line per request instead of the default's timestamp noise.
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the sign-in page.")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler)
    print(f"Sign-in page on http://localhost:{arguments.port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
