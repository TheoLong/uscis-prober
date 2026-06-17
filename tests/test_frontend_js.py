# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bridge: run the Node-based frontend unit tests as part of `pytest -q`.

The dashboard's system-log rendering helpers live in src/static/app.js and are
exercised by tests/frontend/*.test.mjs via Node's built-in test runner (no npm
deps). Running them through pytest keeps a single `pytest -q` entry point for
the whole suite — locally and in CI (GitHub's Ubuntu runners ship Node).

If Node isn't installed, the test skips rather than failing, so a Python-only
environment still gets a green suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_TEST = ROOT / "tests" / "frontend" / "syslog_render.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_frontend_syslog_render_suite_passes():
    assert FRONTEND_TEST.exists(), f"missing {FRONTEND_TEST}"
    result = subprocess.run(
        ["node", "--test", str(FRONTEND_TEST)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        "Node frontend tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
