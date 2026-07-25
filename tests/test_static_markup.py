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


def test_action_buttons_in_expected_order():
    # Actions row order: Redaction, Access Lock, Recompute, Debug, Export data,
    # then the Export log / Clear log mount slot.
    order = [
        'id="redaction-pill"',
        'id="access-lockout-pill"',
        'id="recompute-btn"',
        'id="debug-mode-pill"',
        'id="export-btn"',
        'id="syslog-controls-mount"',
    ]
    positions = [INDEX_HTML.find(tok) for tok in order]
    assert all(p != -1 for p in positions), f"missing control(s): {dict(zip(order, positions))}"
    assert positions == sorted(positions), \
        f"action controls out of order: {dict(zip(order, positions))}"


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


def test_access_lock_pill_present_as_switch_labelled():
    m = re.search(r'id="access-lockout-pill"[^>]*', INDEX_HTML)
    assert m, "access-lockout-pill not found"
    tag = m.group(0)
    assert 'role="switch"' in tag, "access lock pill should be an ARIA switch"
    assert "debug-pill" in tag, "access lock pill should reuse the toggle-pill styling"
    # The visible label is "Access Lock" (not "Access Lockout").
    pill = INDEX_HTML[m.start():]
    label = pill[: pill.find("</button>")]
    assert "Access Lock" in label and "Access Lockout" not in label


def test_guarded_action_buttons_marked_for_redaction_lock():
    # Every action button the redaction lock grays out carries data-guard so
    # the CSS overlay + the JS challenge can find it.
    for token in ('id="pull-btn"', 'id="debug-mode-pill"', 'id="export-btn"',
                  'id="recompute-btn"'):
        start = INDEX_HTML.find(token)
        assert start != -1, f"{token} missing"
        tag = INDEX_HTML[start: INDEX_HTML.find(">", start)]
        assert 'data-guard="redaction"' in tag, f"{token} should carry data-guard"


def test_redaction_lock_overlay_styles_ship():
    assert "redaction-latched" in STYLE_CSS, "missing grayed-lock overlay styles"
    assert ".admin-pw-input" in STYLE_CSS, "missing admin-password prompt input style"


def test_locked_api_pill_does_not_get_double_padlock():
    # One uniform lock mechanism: the API pill is just another
    # [data-guard="redaction"] control, so its single padlock comes from the
    # generic guard overlay. It must NOT also carry its own ::before padlock
    # (that was the double-lock bug). Regression guard.
    css = STYLE_CSS
    # The pill's ::before must not inject a padlock glyph.
    start = css.find(".api-link-locked::before")
    assert start != -1, ".api-link-locked::before rule missing"
    rule = css[start: css.find("}", start)]
    assert "🔒" not in rule, (
        "API pill must not render its own padlock — the generic guard overlay "
        f"is the single lock source. Offending rule: {rule.strip()!r}"
    )
    # And the generic overlay must NOT special-case the pill out (it applies
    # uniformly to every guarded control now).
    assert ':not(.api-link-locked)' not in css, (
        "generic guard overlay should treat .api-link-locked like any other "
        "[data-guard] control — no special-case exclusion"
    )


def test_redaction_popover_explains_what_is_masked():
    pop = INDEX_HTML[INDEX_HTML.find('id="redaction-info-popover"'):]
    pop = pop[: pop.find("</div>") + len("</div>")].lower()
    for term in ("case", "name", "download", "snapshot"):
        assert term in pop, f"redaction popover should mention {term!r}"
    assert "screenshot" in pop or "shar" in pop, "popover should explain the share/screenshot purpose"
    assert "server" in pop, "popover should state masking is server-side"


def test_redaction_styles_ship():
    assert ".redacted-text" in STYLE_CSS, "missing .redacted-text redaction-bar style"
    # The disabled snapshot actions must have a grayed style.
    assert re.search(r"\.raw-btn(:disabled|\.is-disabled)", STYLE_CSS), \
        "missing disabled/is-disabled style for raw buttons"
    # The locked (non-expandable) pull row style.
    assert ".syslog-nested-locked" in STYLE_CSS, "missing locked pull-row style"


def test_diff_recomputed_breakdown_styles_ship():
    # The per-case table classes the renderer emits must be defined.
    for selector in (
        ".diffrc-table", ".diffrc-row", ".diffrc-label",
        ".diffrc-metric", ".diffrc-num", ".diffrc-unit",
    ):
        assert selector in STYLE_CSS, f"missing CSS for {selector}"
    # The row grid is two columns: label + the 'updates' metric.
    row_block = STYLE_CSS[STYLE_CSS.find(".diffrc-row"):]
    row_block = row_block[: row_block.find("}")]
    grid = re.search(r"grid-template-columns:\s*([^;]+);", row_block)
    assert grid, ".diffrc-row should define grid-template-columns"
    columns = re.findall(r"minmax\([^)]*\)|auto|\d+\w*|1fr", grid.group(1))
    assert len(columns) == 2, f"expected 2 columns, got {columns}"
