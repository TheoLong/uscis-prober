# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for event_links: pure re-emit detection, no I/O."""

from event_links import event_links


def _ev(code, eid, event_ts, created_ts, event_dt=None):
    return {
        "eventCode": code,
        "eventId": eid,
        "eventTimestamp": event_ts,
        "createdAtTimestamp": created_ts,
        "eventDateTime": event_dt or event_ts[:10],
    }


def test_no_links_for_unique_events():
    events = [
        _ev("IAF", "a", "2026-02-20T00:00:00.000Z", "2026-03-05T03:23:08.149Z"),
        _ev("FTA0", "b", "2026-03-10T16:59:51.831Z", "2026-03-10T17:08:49.263Z"),
    ]
    assert event_links(events) == []


def test_reemit_links_origin_to_later_row():
    # Same code + same eventTimestamp, different ids; later createdAt = re-emit.
    events = [
        _ev("FTA0", "origin", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.146Z"),
        _ev("FTA0", "reemit", "2026-03-10T16:59:51.837Z", "2026-06-05T13:37:17.302Z"),
    ]
    links = event_links(events)
    assert len(links) == 1
    link = links[0]
    assert link["kind"] == "reemit"
    assert link["eventCode"] == "FTA0"
    assert link["originId"] == "origin"
    assert link["reemitId"] == "reemit"
    assert link["daysApart"] == 86  # whole-day gap 03/10 17:08 -> 06/05 13:37


def test_origin_is_earliest_created_regardless_of_input_order():
    # Re-emit listed FIRST in the input; origin must still be the earlier write.
    events = [
        _ev("FTA1", "reemit", "2026-06-05T13:46:44.620Z", "2026-06-24T17:18:28.250Z"),
        _ev("FTA1", "origin", "2026-06-05T13:46:44.620Z", "2026-06-05T13:47:22.685Z"),
    ]
    links = event_links(events)
    assert len(links) == 1
    assert links[0]["originId"] == "origin"
    assert links[0]["reemitId"] == "reemit"


def test_millisecond_distinct_events_do_not_merge():
    # The 485 trap: three FTA0 rows, all eventDateTime 03/10. Two share the
    # exact eventTimestamp (.837 -> a real re-emit pair); the third sits 6ms
    # away (.831 -> a genuinely separate no-show) and must NOT be linked.
    events = [
        _ev("FTA0", "orig837", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.146Z"),
        _ev("FTA0", "distinct831", "2026-03-10T16:59:51.831Z", "2026-03-10T17:08:49.263Z"),
        _ev("FTA0", "reemit837", "2026-03-10T16:59:51.837Z", "2026-06-05T13:37:17.302Z"),
    ]
    links = event_links(events)
    assert len(links) == 1
    assert {links[0]["originId"], links[0]["reemitId"]} == {"orig837", "reemit837"}
    # The .831 row appears in no link.
    assert all("distinct831" not in (l["originId"], l["reemitId"]) for l in links)


def test_same_day_reemit_zero_days_apart():
    # I-765 case: re-emit written ~4s after origin, same day -> daysApart 0.
    events = [
        _ev("FTA0", "o", "2026-03-10T16:59:50.810Z", "2026-03-10T17:08:52.371Z"),
        _ev("FTA0", "r", "2026-03-10T16:59:50.810Z", "2026-03-10T17:08:56.527Z"),
    ]
    links = event_links(events)
    assert len(links) == 1
    assert links[0]["daysApart"] == 0


def test_three_member_group_yields_two_links_to_one_origin():
    events = [
        _ev("FTA0", "origin", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.000Z"),
        _ev("FTA0", "re1", "2026-03-10T16:59:51.837Z", "2026-05-01T00:00:00.000Z"),
        _ev("FTA0", "re2", "2026-03-10T16:59:51.837Z", "2026-06-05T00:00:00.000Z"),
    ]
    links = event_links(events)
    assert len(links) == 2
    assert all(l["originId"] == "origin" for l in links)
    assert {l["reemitId"] for l in links} == {"re1", "re2"}


def test_different_codes_same_timestamp_do_not_link():
    # Code is part of the key; a shared timestamp across codes must not merge.
    events = [
        _ev("FTA0", "a", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.000Z"),
        _ev("FTA1", "b", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.000Z"),
    ]
    assert event_links(events) == []


def test_missing_event_timestamp_excluded():
    events = [
        _ev("FTA0", "a", "2026-03-10T16:59:51.837Z", "2026-03-10T17:08:49.000Z"),
        {"eventCode": "FTA0", "eventId": "b", "createdAtTimestamp": "2026-06-05T00:00:00Z"},
    ]
    # The row with no eventTimestamp can't be keyed; no link.
    assert event_links(events) == []


def test_empty_and_none_inputs():
    assert event_links([]) == []
    assert event_links(None) == []
