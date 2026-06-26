# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional single-code access gate for the dashboard.

Enabled when `config.auth.optional_access_code` is a non-empty string. Behaviour:
  - Unauthenticated requests (except /login, /api/login, /static/*, /api/auth/*)
    are redirected to the login page (for browser nav) or rejected 401 (API).
  - Successful login sets a signed session cookie with a 30-day lifetime.
  - Brute-force guard: max 5 failed attempts per client IP per 5 minutes.
  - Constant-time code comparison so a wrong code reveals nothing by timing.
  - Flask's SECRET_KEY is persisted to disk so sessions survive server
    restarts; regenerated automatically when the access code changes so
    stale cookies stop working if you rotate the code.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from system_log import log as sys_log

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    session,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 5 * 60
SESSION_DAYS = 30


# ---------------------------------------------------------------------------
# In-memory brute-force tracker. Restart-lossy by design — a server bounce
# wipes the rate limiter, which is acceptable because restart also rotates
# zero session cookies (they're signed with the same key) and the code is
# long/static: an attacker cannot scale meaningfully with 5 attempts per
# 5-minute window per IP.
# ---------------------------------------------------------------------------

class _BruteForceGuard:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _prune(self, q: "deque[float]", now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while q and q[0] < cutoff:
            q.popleft()

    def is_blocked(self, ip: str) -> tuple[bool, int]:
        """(blocked_now, seconds_until_next_allowed)."""
        now = time.time()
        with self._lock:
            q = self._failures[ip]
            self._prune(q, now)
            if len(q) >= MAX_ATTEMPTS:
                retry_after = int(WINDOW_SECONDS - (now - q[0])) + 1
                return True, max(retry_after, 1)
            return False, 0

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            q = self._failures[ip]
            self._prune(q, now)
            q.append(now)

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


_guard = _BruteForceGuard()


# ---------------------------------------------------------------------------
# Shared credential check — used by the login gate AND the per-action admin
# challenge in server.py (toggling latches, actuating buttons while redaction
# is latched). One brute-force guard and one constant-time compare back every
# password prompt in the app, so an attacker can't sidestep the login limiter
# by hammering an action endpoint instead.
# ---------------------------------------------------------------------------

def client_ip() -> str:
    """Best-effort client IP for the current request, proxy-header aware."""
    return (request.headers.get("X-Forwarded-For")
            or request.remote_addr or "?").split(",")[0].strip()


def verify_code(provided: str, expected: str, *, ip: str | None = None) -> tuple[bool, int]:
    """Constant-time check of `provided` against `expected`, brute-force guarded.

    Returns (ok, retry_after_seconds). A non-zero retry_after means the caller
    IP is currently rate-limited and the attempt was not even compared. A wrong
    or empty code records a failure; a correct one clears the IP's failures.
    """
    ip = ip or client_ip()
    blocked, retry_after = _guard.is_blocked(ip)
    if blocked:
        return False, retry_after
    if expected and provided and hmac.compare_digest(provided, expected):
        _guard.clear(ip)
        return True, 0
    _guard.record_failure(ip)
    return False, 0


# ---------------------------------------------------------------------------
# Stable Flask secret key: persist to disk, regenerate when code rotates
# ---------------------------------------------------------------------------

def _fingerprint(optional_access_code: str) -> str:
    return hashlib.sha256(optional_access_code.encode("utf-8")).hexdigest()[:16]


def _load_or_create_secret(root: Path, optional_access_code: str) -> bytes:
    """Load (or create) a 32-byte secret key bound to the current access code.

    If the code changes, the file is rewritten → old cookies invalidate.

    Every distinct failure mode (corrupt file, chmod rejected, write
    refused) emits a `flask_secret_*` sys_log event so the operator can
    tell "I can't load the key because the file is corrupt" from "I
    can't write a new key because the data dir is read-only" — both
    silently drop into the same `except` today without a log.
    """
    secret_file = root / ".flask_secret"
    fp = _fingerprint(optional_access_code)
    if secret_file.exists():
        try:
            raw = secret_file.read_bytes()
            stored_fp, _, key = raw.partition(b"\n")
            if stored_fp.decode("ascii", errors="replace") == fp and key:
                return key
            # Fingerprint mismatch → access code changed; fall through
            # to regenerate. That's expected, not an error.
        except Exception as e:  # noqa: BLE001 — could be OS or decode
            sys_log(
                "flask_secret_read_failed", level="warning",
                source="access_gate", path=str(secret_file),
                error=f"{type(e).__name__}: {e}"[:200],
            )
            # fall through to regenerate below
    try:
        new_key = secrets.token_bytes(32)
        secret_file.write_bytes(fp.encode("ascii") + b"\n" + new_key)
    except Exception as e:  # noqa: BLE001 — disk-full, perms, etc.
        sys_log(
            "flask_secret_write_failed", level="error",
            source="access_gate", path=str(secret_file),
            error=f"{type(e).__name__}: {e}"[:200],
        )
        # Fall back to an in-memory key so the server still boots; the
        # operator will see the event and know cookies won't survive a
        # restart until they fix the disk.
        return secrets.token_bytes(32)
    try:
        secret_file.chmod(0o600)
    except OSError as e:
        sys_log(
            "flask_secret_chmod_failed", level="warning",
            source="access_gate", path=str(secret_file),
            error=f"{type(e).__name__}: {e}"[:200],
        )
    return new_key


# ---------------------------------------------------------------------------
# Login page HTML (inline so no template engine needed)
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>USCIS Prober — Sign in</title>
<link rel="icon" type="image/svg+xml" href="/static/logo.svg">
<link rel="stylesheet" href="/static/style.css">
<style>
  body { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .gate {
    width: 360px;
    padding: 28px 30px 24px;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
  }
  .gate h1 {
    margin: 0 0 4px;
    font-size: 1.05rem; font-weight: 700; letter-spacing: 0.02em;
    display: flex; align-items: center; gap: 8px;
  }
  .gate h1 img { width: 22px; height: 22px; }
  .gate p  { margin: 0 0 20px; color: var(--muted); font-size: 0.85rem; }
  .gate input {
    width: 100%; padding: 10px 12px;
    font: inherit; font-family: var(--font-mono); font-size: 0.95rem;
    color: var(--text);
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    outline: none;
  }
  .gate input:focus { border-color: var(--accent-hot); }
  .gate button {
    width: 100%; margin-top: 14px;
    padding: 10px 12px;
    font: inherit; font-weight: 600;
    color: var(--bg);
    background: var(--accent-hot);
    border: 0; border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .gate button:hover { background: #a7c4ff; }
  .gate .err { color: var(--bad); font-size: 0.82rem; margin-top: 10px; min-height: 1em; }
</style>
</head>
<body>
<form class="gate" id="f" autocomplete="off">
  <h1><img src="/static/logo.svg" alt="">USCIS Prober</h1>
  <p>Enter the site admin password to continue.</p>
  <input name="code" id="code" type="password" autofocus inputmode="text" autocomplete="off"
         spellcheck="false" placeholder="admin password">
  <button type="submit">Sign in</button>
  <div class="err" id="err">__ERR__</div>
</form>
<script>
  const f = document.getElementById("f");
  const err = document.getElementById("err");
  f.addEventListener("submit", async e => {
    e.preventDefault();
    err.textContent = "";
    const code = document.getElementById("code").value;
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (r.ok) {
      const raw = new URLSearchParams(location.search).get("next") || "/";
      // Reject absolute URLs and protocol-relative `//host` paths —
      // both would let an attacker bounce the user off-site post-login.
      const next = (raw.startsWith("/") && !raw.startsWith("//")) ? raw : "/";
      location.href = next;
      return;
    }
    if (r.status === 429) {
      const j = await r.json().catch(() => ({}));
      err.textContent = `Too many attempts — try again in ${j.retryAfter || "a few"} seconds.`;
    } else {
      err.textContent = "Wrong password.";
    }
  });
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Public integration
# ---------------------------------------------------------------------------

def configure(
    app: Flask,
    admin_password: str | None,
    *,
    root: Path,
    is_lockout_enabled=None,
) -> bool:
    """Install the access gate on `app` when `admin_password` is a non-empty string.

    The login routes, signed session, and brute-force guard are always installed
    when a password is set — the password also backs the per-action admin
    challenge, so it must be live even when the site itself is open.

    Whether an unauthenticated request is actually turned away is decided per
    request by `is_lockout_enabled`:
      - None (legacy)        → enforce whenever a password is set (static gate).
      - callable returning   → enforce only while it returns True, so the UI can
        a bool                 toggle the lockout on and off at runtime.

    Returns True if the gate was installed, False if auth is disabled (no password).
    """
    if not admin_password:
        logger.warning(
            "Admin password NOT set. Dashboard is open to every host that "
            "can reach this port and no action is password-gated. Safe for "
            "localhost / SSH-tunnel use; set auth.admin_password in config.json "
            "before exposing the server to a LAN or the public internet."
        )
        return False

    def _lockout_active() -> bool:
        return is_lockout_enabled() if callable(is_lockout_enabled) else True

    app.secret_key = _load_or_create_secret(root, admin_password)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Flask sits behind Caddy (terminates TLS). Honour X-Forwarded-Proto so
    # the session cookie's Secure flag kicks in for real HTTPS connections
    # but still lets local SSH-tunnel access work during dev.
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
    except Exception:
        pass
    app.config["SESSION_COOKIE_SECURE"] = True
    # Cookie lifetime: 30 days. Flask needs session.permanent = True to apply.
    from datetime import timedelta
    app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)

    # Routes that don't require auth.
    # /api/version is intentionally open so deploy-verification scripts
    # (and the topbar chip on the login page) can read the running
    # commit SHA without authenticating. The value is the same info
    # anyone can read on GitHub — we're not leaking anything sensitive.
    OPEN_PATHS = {"/login", "/api/login", "/api/auth/status",
                  "/api/version", "/favicon.ico"}
    # `/api/full-trace/` is open so trace.playwright.dev can fetch
    # `trace.zip` cross-origin. The files served are sandboxed inside
    # `data/full_traces/<dir>/` and path-traversal-guarded; their
    # content is the same diagnostic material the operator is actively
    # debugging, so no new PII surface is created.
    OPEN_PREFIXES = ("/static/", "/api/full-trace/", "/trace-viewer/")

    def _logged_in() -> bool:
        return session.get("authed") is True

    @app.before_request
    def _gate_before_request():
        # Only turn requests away while the lockout latch is active. When it's
        # off the site is open to view; redaction's per-action gate (server.py)
        # still independently password-protects mutating actions.
        if not _lockout_active():
            return None
        path = request.path or "/"
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return None
        if _logged_in():
            return None
        # Not authenticated.
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        # Browser nav: send to login.
        nxt = request.full_path if request.query_string else path
        return redirect(url_for_login(nxt))

    def url_for_login(next_url: str) -> str:
        # Only allow same-origin paths. Reject absolute URLs AND protocol-
        # relative `//host/...` which browsers would resolve off-site.
        safe_next = (
            next_url
            if next_url.startswith("/") and not next_url.startswith("//")
            else "/"
        )
        # Percent-encode so `?`/`&`/`#` in the original path don't get
        # interpreted as separators — otherwise URLSearchParams on the
        # login page would silently truncate anything after the first `&`.
        return f"/login?next={quote(safe_next, safe='/')}"

    @app.route("/login", methods=["GET"])
    def login_page():
        return Response(LOGIN_HTML.replace("__ERR__", ""), mimetype="text/html")

    @app.route("/api/login", methods=["POST"])
    def api_login():
        ip = client_ip()
        body = request.get_json(silent=True) or {}
        provided = (body.get("code") or "").strip()
        if not provided:
            # Distinguish "you sent nothing" from "wrong code", but still
            # count it against the brute-force budget so empty spam is capped.
            _guard.record_failure(ip)
            return jsonify({"ok": False, "error": "missing_code"}), 400

        ok, retry_after = verify_code(provided, admin_password, ip=ip)
        if ok:
            session.permanent = True
            session["authed"] = True
            return jsonify({"ok": True})
        if retry_after:
            resp = jsonify({"ok": False, "error": "rate_limited", "retryAfter": retry_after})
            resp.headers["Retry-After"] = str(retry_after)
            return resp, 429

        # Artificial small delay on failure — cheap defence in depth.
        time.sleep(0.25)
        return jsonify({"ok": False, "error": "bad_code"}), 401

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/auth/status")
    def api_auth_status():
        # authRequired tracks the live lockout latch, not just "a password
        # exists" — the login page polls this to know whether to gate.
        return jsonify({"authRequired": _lockout_active(), "authed": _logged_in()})

    logger.info(
        "Access gate armed (password length=%d, %dd sessions, %d attempts/%ds IP limit).",
        len(admin_password), SESSION_DAYS, MAX_ATTEMPTS, WINDOW_SECONDS,
    )
    return True
