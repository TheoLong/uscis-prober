# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural checks on the System-tab Actions markup in index.html.

These guard the parts of the Recompute-diff feature that live in static HTML
(button placement, styling hooks, and the info popover copy) — the bits the
JS/Node unit tests can't see and that otherwise only the browser e2e covers.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "static"
INDEX_HTML = (STATIC / "index.html").read_text()
STYLE_CSS = (STATIC / "style.css").read_text()


def test_recompute_button_present_with_secondary_styling():
    # Matches the Export-data button's styling hooks so the two look alike.
    m = re.search(r'id="recompute-btn"[^>]*class="([^"]*)"', INDEX_HTML)
    assert m, "recompute-btn not found"
    classes = m.group(1).split()
    for cls in ("action-btn", "pull-btn", "pull-btn-secondary"):
        assert cls in classes, f"recompute-btn missing styling class {cls!r}"


def test_recompute_button_sits_immediately_right_of_export():
    export_at = INDEX_HTML.find('id="export-btn"')
    recompute_at = INDEX_HTML.find('id="recompute-btn"')
    mount_at = INDEX_HTML.find('id="syslog-controls-mount"')
    assert -1 < export_at < recompute_at, "recompute-btn must come after export-btn"
    # ...and before the syslog controls mount slot, i.e. directly adjacent.
    assert recompute_at < mount_at, "recompute-btn must sit before the syslog controls mount"
    # Recompute's wrapper is the very next action block after Export's:
    # Export's button lives inside the prior `action-with-info`, so the only
    # such opening between the two buttons is Recompute's own.
    between = INDEX_HTML[export_at:recompute_at]
    assert between.count('class="action-with-info"') == 1, \
        "another action block is wedged between Export and Recompute"


def test_recompute_info_badge_and_popover_present_and_wired():
    assert 'id="recompute-info-btn"' in INDEX_HTML
    assert 'id="recompute-info-popover"' in INDEX_HTML
    # ARIA wiring: the badge controls the popover.
    assert 'aria-controls="recompute-info-popover"' in INDEX_HTML


def test_recompute_popover_explains_the_action():
    popover = INDEX_HTML[INDEX_HTML.find('id="recompute-info-popover"'):]
    popover = popover[: popover.find("</div>") + len("</div>")].lower()
    assert "cached diff" in popover, "popover should mention clearing cached diffs"
    assert "chronological diff" in popover, "popover should mention the chronological diff trace"
    assert "diff_recomputed" in popover, "popover should name the logged event"


def test_redaction_pill_present_as_switch():
    m = re.search(r'id="redaction-pill"[^>]*', INDEX_HTML)
    assert m, "redaction-pill not found"
    tag = m.group(0)
    assert 'role="switch"' in tag, "redaction pill should be an ARIA switch"
    assert "debug-pill" in tag, "redaction pill should reuse the toggle-pill styling"


def test_redaction_info_badge_and_popover_present_and_wired():
    assert 'id="redaction-info-btn"' in INDEX_HTML
    assert 'id="redaction-info-popover"' in INDEX_HTML
    assert 'aria-controls="redaction-info-popover"' in INDEX_HTML


def test_redaction_popover_explains_what_is_masked():
    pop = INDEX_HTML[INDEX_HTML.find('id="redaction-info-popover"'):]
    pop = pop[: pop.find("</div>") + len("</div>")].lower()
    for term in ("case", "name", "download", "snapshot"):
        assert term in pop, f"redaction popover should mention {term!r}"
    assert "screenshot" in pop or "shar" in pop, "popover should explain the share/screenshot purpose"


def test_redaction_styles_ship():
    assert ".redacted-text" in STYLE_CSS, "missing .redacted-text redaction-bar style"
    # The disabled snapshot actions must have a grayed style.
    assert re.search(r"\.raw-btn(:disabled|\.is-disabled)", STYLE_CSS), \
        "missing disabled/is-disabled style for raw buttons"


def test_diff_recomputed_breakdown_styles_ship():
    # The per-case table classes the renderer emits must be defined.
    for selector in (
        ".diffrc-table", ".diffrc-row", ".diffrc-label",
        ".diffrc-metric", ".diffrc-num", ".diffrc-unit",
    ):
        assert selector in STYLE_CSS, f"missing CSS for {selector}"
    # The row grid is two columns now (label + single 'updates' metric),
    # not the original three (case / location).
    row_block = STYLE_CSS[STYLE_CSS.find(".diffrc-row"):]
    row_block = row_block[: row_block.find("}")]
    grid = re.search(r"grid-template-columns:\s*([^;]+);", row_block)
    assert grid, ".diffrc-row should define grid-template-columns"
    columns = re.findall(r"minmax\([^)]*\)|auto|\d+\w*|1fr", grid.group(1))
    assert len(columns) == 2, f"expected 2 columns, got {columns}"
