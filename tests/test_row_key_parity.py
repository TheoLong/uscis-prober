# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract test: the composite event ROW KEY must be computed identically by
the Python server (event_links._row_key) and the JS frontend (rowKeyOf in
app.js).

Both sides key the reemit link overlay + timeline dedup on this composite
natural key (eventCode|eventTimestamp|createdAtTimestamp) precisely so every
identifier can be fully masked in redaction / demo output. If the two
implementations ever drift — different field order, different join char,
different null handling — the overlay silently stops drawing and dedup breaks,
with no other test catching it. This test runs BOTH implementations against one
shared fixture and asserts byte-for-byte equality, so any drift fails CI.

The fixture is the single source of truth; neither side hand-copies expected
strings (which would themselves be able to drift). The assertion is that
Python's output list == JS's output list.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from event_links import _row_key  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "row_key_cases.json"
HARNESS = ROOT / "tests" / "frontend" / "row_key_harness.mjs"


def _python_keys() -> list[str]:
    cases = json.loads(FIXTURE.read_text())["cases"]
    return [_row_key(ev) for ev in cases]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_row_key_python_and_js_agree():
    py_keys = _python_keys()
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        "row_key_harness.mjs failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    js_keys = json.loads(result.stdout)

    # Byte-for-byte parity across every fixture case. Reported per-index so a
    # drift points straight at the offending event shape.
    assert len(py_keys) == len(js_keys), (
        f"case count differs: py={len(py_keys)} js={len(js_keys)}"
    )
    mismatches = [
        (i, p, j) for i, (p, j) in enumerate(zip(py_keys, js_keys)) if p != j
    ]
    assert not mismatches, (
        "Python _row_key and JS rowKeyOf disagree — the reemit overlay would "
        "mis-wire. Mismatches (index, python, js):\n"
        + "\n".join(f"  [{i}] {p!r} != {j!r}" for i, p, j in mismatches)
    )


def test_row_key_shape_is_pii_free():
    # Guard the contract's premise: the key is built only from non-PII fields
    # (form code + timestamps). If someone adds a receipt/name field to the
    # composite, this fails — because the key is preserved through redaction and
    # would then leak.
    key = _row_key({
        "eventCode": "FTA0",
        "eventTimestamp": "2026-03-10T16:59:51.837Z",
        "createdAtTimestamp": "2026-03-10T17:08:49.146Z",
        "receiptNumber": "IOE0000000000",
        "applicantName": "SHOULD NOT APPEAR",
    })
    assert "IOE0000000000" not in key
    assert "SHOULD NOT APPEAR" not in key
    assert key == "FTA0|2026-03-10T16:59:51.837Z|2026-03-10T17:08:49.146Z"
