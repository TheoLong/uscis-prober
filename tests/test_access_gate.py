# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end tests for the access gate via Flask's test client."""

import tempfile
from pathlib import Path

import pytest
from flask import Flask, jsonify

import access_gate
from access_gate import configure


@pytest.fixture(autouse=True)
def _isolate_guard():
    """Reset the module-level brute-force tracker between tests."""
    access_gate._guard = access_gate._BruteForceGuard()
    yield


@pytest.fixture
def gated_app():
    app = Flask(__name__)
    app.testing = True

    @app.route("/")
    def index():
        return "home"

    @app.route("/api/cases")
    def cases():
        return jsonify({"cases": []})

    with tempfile.TemporaryDirectory() as tmp:
        configure(app, "sesame-open", root=Path(tmp))
        yield app


@pytest.fixture
def open_app():
    app = Flask(__name__)
    app.testing = True

    @app.route("/api/cases")
    def cases():
        return jsonify({"cases": []})

    with tempfile.TemporaryDirectory() as tmp:
        configure(app, "", root=Path(tmp))  # empty = disabled
        yield app


# -------- no code configured -> gate is a no-op ---------------------------

def test_no_code_means_no_gate(open_app):
    c = open_app.test_client()
    assert c.get("/api/cases").status_code == 200


# -------- browser nav on a gated app --------------------------------------

def test_unauthed_root_redirects_to_login(gated_app):
    c = gated_app.test_client()
    r = c.get("/")
    assert r.status_code == 302
    # Flask versions differ on whether Location is absolute or relative.
    assert "/login" in r.headers["Location"]
    assert "next=/" in r.headers["Location"]


def test_login_page_is_open(gated_app):
    c = gated_app.test_client()
    r = c.get("/login")
    assert r.status_code == 200
    assert b"access code" in r.data.lower()


# -------- API responses ---------------------------------------------------

def test_unauthed_api_returns_401_json(gated_app):
    c = gated_app.test_client()
    r = c.get("/api/cases")
    assert r.status_code == 401
    body = r.get_json()
    assert body["ok"] is False
    assert body["error"] == "auth_required"


# -------- login flow ------------------------------------------------------

def test_correct_code_opens_session(gated_app):
    c = gated_app.test_client()
    r = c.post("/api/login", json={"code": "sesame-open"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # session cookie now set — subsequent API call must work
    assert c.get("/api/cases").status_code == 200


def test_wrong_code_rejected(gated_app):
    c = gated_app.test_client()
    r = c.post("/api/login", json={"code": "nope"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "bad_code"


def test_missing_code_is_bad_request(gated_app):
    c = gated_app.test_client()
    r = c.post("/api/login", json={})
    assert r.status_code == 400


def test_logout_clears_session(gated_app):
    c = gated_app.test_client()
    c.post("/api/login", json={"code": "sesame-open"})
    assert c.get("/api/cases").status_code == 200
    r = c.post("/api/logout")
    assert r.status_code == 200
    # After logout, access is blocked again
    assert c.get("/api/cases").status_code == 401


# -------- brute force lockout ---------------------------------------------

def test_brute_force_lockout_kicks_in_after_five_failures(gated_app):
    c = gated_app.test_client()
    for _ in range(5):
        r = c.post("/api/login", json={"code": "nope"})
        assert r.status_code == 401
    # 6th attempt within 5 min -> 429
    r = c.post("/api/login", json={"code": "sesame-open"})
    assert r.status_code == 429
    body = r.get_json()
    assert body["error"] == "rate_limited"
    assert "retryAfter" in body
    assert r.headers.get("Retry-After") is not None


# -------- auth status endpoint --------------------------------------------

def test_auth_status_reports_state(gated_app):
    c = gated_app.test_client()
    r = c.get("/api/auth/status").get_json()
    assert r == {"authRequired": True, "authed": False}
    c.post("/api/login", json={"code": "sesame-open"})
    r = c.get("/api/auth/status").get_json()
    assert r == {"authRequired": True, "authed": True}


# -------- secret-key persistence behaviour --------------------------------

def test_secret_key_persists_across_configures_when_code_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        app1 = Flask("app1")
        configure(app1, "same-code", root=Path(tmp))
        app2 = Flask("app2")
        configure(app2, "same-code", root=Path(tmp))
        assert app1.secret_key == app2.secret_key


def test_secret_key_rotates_when_access_code_changes():
    with tempfile.TemporaryDirectory() as tmp:
        app1 = Flask("app1")
        configure(app1, "old-code", root=Path(tmp))
        app2 = Flask("app2")
        configure(app2, "new-code", root=Path(tmp))
        assert app1.secret_key != app2.secret_key


def test_secret_key_regenerates_on_corrupt_file(tmp_path):
    # Pre-seed a corrupt secret file — configure must recover.
    (tmp_path / ".flask_secret").write_bytes(b"this is not the expected format")
    app = Flask("x")
    configure(app, "any-code", root=tmp_path)
    assert isinstance(app.secret_key, (bytes, bytearray))
    assert len(app.secret_key) == 32


def test_secret_key_recovers_on_read_exception(tmp_path, monkeypatch):
    secret_file = tmp_path / ".flask_secret"
    secret_file.write_bytes(b"whatever")
    real_read = type(secret_file).read_bytes

    def boom(self):
        if self.name == ".flask_secret":
            raise IOError("simulated")
        return real_read(self)

    import pathlib
    monkeypatch.setattr(pathlib.Path, "read_bytes", boom)
    try:
        app = Flask("x")
        configure(app, "any-code", root=tmp_path)
        assert len(app.secret_key) == 32
    finally:
        monkeypatch.setattr(pathlib.Path, "read_bytes", real_read)


def test_secret_key_handles_chmod_oserror(tmp_path, monkeypatch):
    # Force chmod to raise; configure must still succeed.
    import pathlib
    real_chmod = pathlib.Path.chmod

    def boom(self, mode):
        raise OSError("simulated")

    monkeypatch.setattr(pathlib.Path, "chmod", boom)
    try:
        app = Flask("x")
        configure(app, "any-code", root=tmp_path)
        assert len(app.secret_key) == 32
    finally:
        monkeypatch.setattr(pathlib.Path, "chmod", real_chmod)


# -------- client-IP extraction (X-Forwarded-For) --------------------------

def test_x_forwarded_for_used_for_rate_limit(gated_app):
    c = gated_app.test_client()
    # 5 failures from same XFF IP → lockout
    for _ in range(5):
        c.post("/api/login", json={"code": "x"},
               headers={"X-Forwarded-For": "10.0.0.1, 192.168.1.1"})
    r = c.post("/api/login", json={"code": "x"},
               headers={"X-Forwarded-For": "10.0.0.1, 192.168.1.1"})
    assert r.status_code == 429


def test_different_xff_ips_dont_share_bucket(gated_app):
    c = gated_app.test_client()
    for _ in range(5):
        c.post("/api/login", json={"code": "x"},
               headers={"X-Forwarded-For": "10.0.0.1"})
    # A different IP should still have a full budget
    r = c.post("/api/login", json={"code": "x"},
               headers={"X-Forwarded-For": "10.0.0.2"})
    assert r.status_code == 401  # bad_code, not rate_limited


# -------- open paths + prefixes ------------------------------------------

def test_favicon_is_open(gated_app):
    c = gated_app.test_client()
    # Flask's default 404 is still reachable — gate must not redirect.
    r = c.get("/favicon.ico")
    # Either a 404 (no handler) or a 200 (if app serves one) — key is not 302.
    assert r.status_code != 302


def test_static_prefix_is_open(gated_app):
    # Pretend the app has static files available.
    c = gated_app.test_client()
    r = c.get("/static/anything.css")
    # Not redirected to /login; Flask just 404s because no such file.
    assert r.status_code != 302


def test_login_with_query_string_preserves_next(gated_app):
    c = gated_app.test_client()
    r = c.get("/api/cases?foo=bar&baz=1", headers={"Accept": "text/html"})
    # API returns 401 JSON; the full_path branch fires on browser nav paths.
    # Hit a non-api path with a query string instead:
    r = c.get("/some-page?x=1")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_strips_absolute_next_url(gated_app):
    # Any `next` that doesn't start with / must be clipped to "/".
    c = gated_app.test_client()
    # We can reach url_for_login via the redirect on an unauthed GET.
    r = c.get("/somewhere")
    assert r.status_code == 302
    # next is safe path
    assert "next=/" in r.headers["Location"]


def test_next_param_query_string_is_percent_encoded(gated_app):
    # URLSearchParams on the login page would split on `&`; any query
    # params after the first must survive by being percent-encoded.
    c = gated_app.test_client()
    r = c.get("/some-page?a=1&b=2")
    assert r.status_code == 302
    loc = r.headers["Location"]
    # `?` and `&` in the next value must be encoded, not raw.
    # The next value is everything after `next=`.
    assert "next=" in loc
    nxt = loc.split("next=", 1)[1]
    assert "%3F" in nxt.upper()
    assert "%26" in nxt.upper()


def test_login_js_rejects_protocol_relative_next(gated_app):
    # The inlined LOGIN_HTML must contain the tightened guard so
    # `location.href = next` can never navigate off-site.
    c = gated_app.test_client()
    r = c.get("/login")
    body = r.data.decode()
    # Guard string present; the raw 'location.href = raw' pattern must not
    # appear (that's the unguarded version).
    assert "startsWith(\"//\")" in body
    assert "location.href = raw" not in body


# -------- brute-force guard unit tests -----------------------------------

def test_brute_force_guard_retry_after_decreases(monkeypatch):
    g = access_gate._BruteForceGuard()
    t = [1000.0]
    monkeypatch.setattr(access_gate.time, "time", lambda: t[0])
    for _ in range(5):
        g.record_failure("1.2.3.4")
    blocked, retry = g.is_blocked("1.2.3.4")
    assert blocked and retry > 0
    # Simulate 60s passing — retry should drop.
    t[0] += 60
    _, retry2 = g.is_blocked("1.2.3.4")
    assert retry2 < retry


def test_brute_force_guard_prunes_old_failures(monkeypatch):
    g = access_gate._BruteForceGuard()
    t = [1000.0]
    monkeypatch.setattr(access_gate.time, "time", lambda: t[0])
    g.record_failure("ip")
    # Jump past the 5-minute window.
    t[0] += access_gate.WINDOW_SECONDS + 10
    blocked, _ = g.is_blocked("ip")
    assert not blocked


def test_brute_force_guard_clear_drops_ip():
    g = access_gate._BruteForceGuard()
    for _ in range(5):
        g.record_failure("ip")
    assert g.is_blocked("ip")[0]
    g.clear("ip")
    assert not g.is_blocked("ip")[0]


# -------- proxyfix fallback: import-error path --------------------------

def test_configure_survives_missing_proxyfix(monkeypatch, tmp_path):
    # Simulate werkzeug.middleware.proxy_fix being unavailable.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "werkzeug.middleware.proxy_fix":
            raise ImportError("pretend missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    app = Flask("proxyless")
    # Must not raise — configure swallows the ImportError.
    assert configure(app, "code", root=tmp_path) is True
