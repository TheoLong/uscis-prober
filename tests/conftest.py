# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test fixtures: add `src/` to sys.path so test files import modules."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _isolate_system_log(monkeypatch, tmp_path):
    """Redirect every test's sys_log writes to a per-test tmp file.

    Without this, any test that imports auth / mfa / server code and
    triggers a sys_log call leaks events into the real
    data/system_log.json, polluting the dashboard. Autouse + top-level
    conftest means every test file is covered — no fixture to opt in.
    """
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "_syslog.json")
    system_log.clear()
