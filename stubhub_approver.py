"""Local one-click approve server for the StubHub repricer.

Turns the "✅ Approve & apply" link in recommendation emails into an actual price
change. Binds 127.0.0.1 ONLY (never exposed to the network) and requires a secret
token. On a valid /approve request it runs `stubhub_repricer.py --apply <key>` in
the background (which re-checks the live market + StubHub's payout and refuses to
net below your cost) and emails you the result.

Runs as a launchd KeepAlive agent (see scripts/install_stubhub_approver.sh). It
only works while this Mac is awake and you click the link on this Mac.

Env (from ~/.config/stubhub-repricer/env):
    STUBHUB_APPROVE_TOKEN   required secret (refuses to start without it)
    STUBHUB_APPROVE_PORT    default 8765
    GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO  for the result email
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "bin" / "python"
PORT = int(os.environ.get("STUBHUB_APPROVE_PORT", "8765"))
TOKEN = os.environ.get("STUBHUB_APPROVE_TOKEN", "").strip()

_inflight: set[str] = set()
_lock = threading.Lock()


def _run_apply(key: str) -> None:
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(REPO / "stubhub_repricer.py"), "--apply", key],
            capture_output=True, text=True, timeout=600, cwd=str(REPO),
        )
        body = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ok = "✓ list set" in body
    except Exception as exc:  # noqa: BLE001
        body, ok = f"apply crashed: {exc}", False
    try:
        import emailer
        subj = f"[StubHub Repricer] {'✓ applied' if ok else '⚠ apply issue'}: {key}"
        tail = body[-3000:]
        emailer.send(subj, tail, f"<pre style='font-size:12px'>{_esc(tail)}</pre>")
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        _inflight.discard(key)


def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, html: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<html><body style='font-family:-apple-system,sans-serif'>"
                          + html + "</body></html>").encode())

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path != "/approve":
            return self._send(404, "<h2>Not found</h2>")
        q = urllib.parse.parse_qs(u.query)
        key = (q.get("key") or [""])[0]
        token = (q.get("token") or [""])[0]
        if not TOKEN or token != TOKEN:
            return self._send(403, "<h2>Forbidden</h2><p>Invalid or missing token.</p>")
        if not re.fullmatch(r"[A-Za-z0-9_]+", key or ""):
            return self._send(400, "<h2>Bad request</h2><p>Invalid listing key.</p>")
        with _lock:
            if key in _inflight:
                return self._send(200, f"<h2>⏳ Already applying <code>{_esc(key)}</code></h2>"
                                       "<p>An apply for this listing is already running.</p>")
            _inflight.add(key)
        threading.Thread(target=_run_apply, args=(key,), daemon=True).start()
        return self._send(200,
                          f"<h2>✅ Approval received for <code>{_esc(key)}</code></h2>"
                          "<p>Applying the recommended price now — it re-checks the live market "
                          "and refuses to net below your cost. You'll get a confirmation email in "
                          "about a minute. You can close this tab.</p>")

    def log_message(self, *args) -> None:  # silence default logging
        return


def main() -> int:
    if not TOKEN:
        raise SystemExit("STUBHUB_APPROVE_TOKEN not set; refusing to start "
                         "(run scripts/install_stubhub_approver.sh).")
    print(f"stubhub-approver listening on http://127.0.0.1:{PORT}/approve")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
