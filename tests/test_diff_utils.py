# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for diff_utils: pure functions, no I/O."""

from diff_utils import (
    CANONICAL_STAGES,
    bin_by_day,
    classify_change,
    compute_change,
    infer_stage,
    location_snapshot_changes,
    snapshot_changes,
    stage_progression,
    summarize_case,
)


# -------- fixtures ---------------------------------------------------------

def _entry(captured_at: str, **data_overrides):
    base = {
        "receiptNumber": "IOE0000000001",
        "submissionDate": "2026-02-20",
        "submissionTimestamp": "2026-02-20T00:00:00.000Z",
        "formType": "I-485",
        "updatedAt": "2026-02-20",
        "updatedAtTimestamp": "2026-02-20T00:00:00.000Z",
        "closed": False,
        "actionRequired": False,
        "ackedByAdjudicatorAndCms": True,
        "events": [],
        "notices": [],
        "documents": [],
        "evidenceRequests": [],
        "addendums": [],
    }
    base.update(data_overrides)
    return {"capturedAt": captured_at, "data": base}


# -------- bin_by_day -------------------------------------------------------

def test_bin_by_day_keeps_latest_of_day():
    entries = [
        _entry("2026-03-10T10:00:00Z", updatedAt="2026-03-05"),
        _entry("2026-03-10T23:59:59Z", updatedAt="2026-03-10"),  # winner
        _entry("2026-03-11T01:00:00Z", updatedAt="2026-03-10"),
    ]
    days = bin_by_day(entries)
    assert len(days) == 2
    assert days[0]["capturedAt"] == "2026-03-10T23:59:59Z"
    assert days[1]["capturedAt"] == "2026-03-11T01:00:00Z"


def test_bin_by_day_preserves_chronological_order():
    entries = [
        _entry("2026-03-12T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z"),
        _entry("2026-03-11T00:00:00Z"),
    ]
    days = bin_by_day(entries)
    assert [d["capturedAt"][:10] for d in days] == [
        "2026-03-10",
        "2026-03-11",
        "2026-03-12",
    ]


# -------- classify_change --------------------------------------------------

def test_classify_change_decision_takes_precedence():
    change = {
        "scalars": {"closed": {"from": False, "to": True}},
        "events": {"added": [{"eventCode": "APR0"}], "removed": []},
    }
    assert classify_change(change) == "decision"


def test_classify_change_event():
    change = {
        "scalars": {},
        "events": {"added": [{"eventCode": "FTA0"}], "removed": []},
    }
    assert classify_change(change) == "event"


def test_classify_change_appointment():
    change = {
        "scalars": {},
        "events": {"added": [], "removed": []},
        "notices": {
            "added": [{"actionType": "Appointment Scheduled",
                       "appointmentDateTime": "2026-04-01T13:00:00Z"}],
            "removed": [],
        },
    }
    assert classify_change(change) == "appointment"


def test_classify_change_silent_update_when_updatedAt_date_advances():
    change = {
        "scalars": {
            "updatedAt": {"from": "2026-03-05", "to": "2026-04-07"},
            "updatedAtTimestamp": {"from": "2026-03-05T00:00:00Z",
                                   "to": "2026-04-07T04:30:00Z"},
        },
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
    }
    assert classify_change(change) == "silent_update"


def test_classify_change_silent_update_when_only_timestamp_moves():
    # A change whose only diff is the case update timestamp is a silent update.
    change = {
        "scalars": {
            "updatedAtTimestamp": {"from": "2026-03-10T14:59:52Z",
                                   "to": "2026-03-10T16:59:58Z"},
        },
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
    }
    assert classify_change(change) == "silent_update"


# -------- compute_change + snapshot_changes -------------------------------

def test_compute_change_detects_new_event():
    prev = _entry("2026-03-09T12:00:00Z", events=[{"eventCode": "IAF", "eventId": "a"}])
    curr = _entry(
        "2026-03-10T12:00:00Z",
        events=[
            {"eventCode": "IAF", "eventId": "a"},
            {"eventCode": "FTA0", "eventId": "b"},
        ],
    )
    change = compute_change(prev, curr)
    added = change["events"]["added"]
    assert len(added) == 1
    assert added[0]["eventCode"] == "FTA0"


def test_snapshot_changes_filters_out_unchanged_day_pairs():
    entries = [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z"),  # identical -> no diff
        _entry("2026-03-11T00:00:00Z", closed=True),  # decision flip
    ]
    changes = snapshot_changes(entries)
    assert len(changes) == 1
    assert changes[0]["kind"] == "decision"


def test_snapshot_changes_does_not_collapse_same_day_transitions():
    """A silent update and a new event on the same UTC day surface as two
    separate change records, not one."""
    entries = [
        # Yesterday's settled state.
        _entry(
            "2026-06-24T18:00:00Z",
            updatedAt="2026-06-24",
            updatedAtTimestamp="2026-06-24T18:00:00Z",
            events=[{"eventCode": "FTA0", "eventId": "a"}],
        ),
        # Morning: updatedAt advanced, no new event yet -> silent_update.
        _entry(
            "2026-06-25T14:00:00Z",
            updatedAt="2026-06-25",
            updatedAtTimestamp="2026-06-25T14:00:00Z",
            events=[{"eventCode": "FTA0", "eventId": "a"}],
        ),
        # Afternoon, same UTC day: a new FTA1 event appears -> event.
        _entry(
            "2026-06-25T17:00:00Z",
            updatedAt="2026-06-25",
            updatedAtTimestamp="2026-06-25T17:00:00Z",
            events=[
                {"eventCode": "FTA0", "eventId": "a"},
                {"eventCode": "FTA1", "eventId": "b"},
            ],
        ),
    ]
    changes = snapshot_changes(entries)
    kinds = [c["kind"] for c in changes]
    assert kinds == ["silent_update", "event"]
    # Both anchored on the same calendar day but distinct capture timestamps.
    assert changes[0]["to"] == "2026-06-25T14:00:00Z"
    assert changes[1]["to"] == "2026-06-25T17:00:00Z"
    # The event record carries the newly-added FTA1.
    assert [e["eventCode"] for e in changes[1]["events"]["added"]] == ["FTA1"]


def test_snapshot_changes_keeps_event_footprint_bump():
    """A timestamp-only bump trailing a new event (the case timestamp catching
    up to the event's time in a later pull) is a real, distinct change and must
    be surfaced — the event's own pair did not record it, so dropping it would
    lose the latest updatedAtTimestamp."""
    entries = [
        # Settled prior state.
        _entry(
            "2026-06-25T14:00:00Z",
            updatedAt="2026-06-25",
            updatedAtTimestamp="2026-06-25T13:00:00.000Z",
            events=[{"eventCode": "FTA0", "eventId": "a",
                     "eventTimestamp": "2026-03-10T17:00:00.000Z",
                     "createdAtTimestamp": "2026-03-10T17:00:00.000Z"}],
        ),
        # Pull that first sees the new FTA1 (event row arrives).
        _entry(
            "2026-06-25T14:33:46Z",
            updatedAt="2026-06-25",
            updatedAtTimestamp="2026-06-25T13:00:00.000Z",
            events=[{"eventCode": "FTA0", "eventId": "a",
                     "eventTimestamp": "2026-03-10T17:00:00.000Z",
                     "createdAtTimestamp": "2026-03-10T17:00:00.000Z"},
                    {"eventCode": "FTA1", "eventId": "b",
                     "eventTimestamp": "2026-06-25T15:00:00.000Z",
                     "createdAtTimestamp": "2026-06-25T15:00:01.000Z"}],
        ),
        # Later pull: case updatedAtTimestamp catches up to the FTA1's time.
        # This is a real change and must be its own silent_update row.
        _entry(
            "2026-06-25T15:42:40Z",
            updatedAt="2026-06-25",
            updatedAtTimestamp="2026-06-25T15:00:00.500Z",
            events=[{"eventCode": "FTA0", "eventId": "a",
                     "eventTimestamp": "2026-03-10T17:00:00.000Z",
                     "createdAtTimestamp": "2026-03-10T17:00:00.000Z"},
                    {"eventCode": "FTA1", "eventId": "b",
                     "eventTimestamp": "2026-06-25T15:00:00.000Z",
                     "createdAtTimestamp": "2026-06-25T15:00:01.000Z"}],
        ),
    ]
    changes = snapshot_changes(entries)
    # The FTA1 event row, then the trailing timestamp catch-up — both kept.
    assert [c["kind"] for c in changes] == ["event", "silent_update"]
    assert [e["eventCode"] for e in changes[0]["events"]["added"]] == ["FTA1"]
    assert changes[1]["scalars"]["updatedAtTimestamp"]["to"] == "2026-06-25T15:00:00.500Z"


def test_snapshot_changes_keeps_backdated_event_footprint_bump():
    """A backdated re-emit carries an old eventTimestamp but a recent
    createdAtTimestamp; USCIS stamps the case from the WRITE time. The trailing
    catch-up bump is still a real change and is kept as its own row."""
    entries = [
        _entry(
            "2026-06-05T11:00:00Z",
            updatedAt="2026-04-29",
            updatedAtTimestamp="2026-04-29T05:00:00.000Z",
            events=[{"eventCode": "IAF", "eventId": "a",
                     "eventTimestamp": "2026-02-20T00:00:00.000Z",
                     "createdAtTimestamp": "2026-03-05T03:00:00.000Z"}],
        ),
        # Backdated FTA0 re-emit: eventTimestamp is months old (03-10), but it
        # was written now (06-05). The case timestamp moves to the write time.
        _entry(
            "2026-06-05T13:47:55Z",
            updatedAt="2026-06-05",
            updatedAtTimestamp="2026-06-05T13:30:00.000Z",
            events=[{"eventCode": "IAF", "eventId": "a",
                     "eventTimestamp": "2026-02-20T00:00:00.000Z",
                     "createdAtTimestamp": "2026-03-05T03:00:00.000Z"},
                    {"eventCode": "FTA0", "eventId": "b",
                     "eventTimestamp": "2026-03-10T17:00:00.000Z",
                     "createdAtTimestamp": "2026-06-05T13:30:00.000Z"}],
        ),
        # Trailing bump as the case timestamp catches up to the write time —
        # a real change, kept as its own silent_update row.
        _entry(
            "2026-06-05T18:00:00Z",
            updatedAt="2026-06-05",
            updatedAtTimestamp="2026-06-05T13:30:01.000Z",
            events=[{"eventCode": "IAF", "eventId": "a",
                     "eventTimestamp": "2026-02-20T00:00:00.000Z",
                     "createdAtTimestamp": "2026-03-05T03:00:00.000Z"},
                    {"eventCode": "FTA0", "eventId": "b",
                     "eventTimestamp": "2026-03-10T17:00:00.000Z",
                     "createdAtTimestamp": "2026-06-05T13:30:00.000Z"}],
        ),
    ]
    changes = snapshot_changes(entries)
    assert [c["kind"] for c in changes] == ["event", "silent_update"]


def test_snapshot_changes_keeps_silent_update_far_from_any_event():
    """A timestamp-only bump that is NOT near any event's time is a genuine
    silent update and stays its own row."""
    entries = [
        _entry(
            "2026-06-12T11:00:00Z",
            updatedAt="2026-06-05",
            updatedAtTimestamp="2026-06-05T14:00:00.000Z",
            events=[{"eventCode": "FTA1", "eventId": "b",
                     "eventTimestamp": "2026-06-05T13:00:00.000Z",
                     "createdAtTimestamp": "2026-06-05T13:00:00.000Z"}],
        ),
        # updatedAt date advances a week later, far from the FTA1's time.
        _entry(
            "2026-06-12T18:00:00Z",
            updatedAt="2026-06-12",
            updatedAtTimestamp="2026-06-12T14:00:00.000Z",
            events=[{"eventCode": "FTA1", "eventId": "b",
                     "eventTimestamp": "2026-06-05T13:00:00.000Z",
                     "createdAtTimestamp": "2026-06-05T13:00:00.000Z"}],
        ),
    ]
    changes = snapshot_changes(entries)
    assert len(changes) == 1
    assert changes[0]["kind"] == "silent_update"


def test_snapshot_changes_orders_unsorted_captures():
    """Captures arriving out of order are diffed in capturedAt order, so a
    shuffled input still produces the correct chronological feed."""
    a = _entry("2026-03-09T00:00:00Z")
    b = _entry("2026-03-10T00:00:00Z", closed=True)
    c = _entry("2026-03-11T00:00:00Z", actionRequired=True)
    changes = snapshot_changes([c, a, b])  # shuffled
    assert [ch["to"][:10] for ch in changes] == ["2026-03-10", "2026-03-11"]


# -------- infer_stage / stage_progression ---------------------------------

def test_infer_stage_maps_codes_to_latest_milestone():
    assert infer_stage([]) == "Pending receipt"
    assert infer_stage([{"eventCode": "IAF"}]) == "Received"
    assert infer_stage([{"eventCode": "IAF"}, {"eventCode": "FTA0"}]) == "Biometrics & checks done"
    assert infer_stage([{"eventCode": "APR0"}]) == "Approved"


def test_infer_stage_divergence_takes_precedence_over_happy_path():
    # Request-for-Evidence trumps earlier stages on the happy path.
    assert infer_stage([{"eventCode": "FTA0"}, {"eventCode": "RFE0"}]) == "Request for Evidence outstanding"
    assert infer_stage([{"eventCode": "PRB0"}, {"eventCode": "DNY0"}]) == "Denied"


def test_stage_progression_marks_passed_current_upcoming():
    prog = stage_progression([{"eventCode": "IAF"}, {"eventCode": "FTA0"}])
    assert prog["current"] == "Biometrics & checks done"
    labels = [s["label"] for s in prog["steps"]]
    states = [s["state"] for s in prog["steps"]]
    assert labels == list(CANONICAL_STAGES)
    assert states[0] == "passed"
    assert states[1] == "current"
    assert all(s == "upcoming" for s in states[2:])


# -------- summarize_case ---------------------------------------------------

def test_summarize_case_counts_silent_updates():
    entries = [
        # day 1: received
        _entry(
            "2026-03-05T12:00:00Z",
            updatedAt="2026-03-05",
            updatedAtTimestamp="2026-03-05T10:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
        # day 2: updatedAt date advances, no new events (silent update)
        _entry(
            "2026-03-06T12:00:00Z",
            updatedAt="2026-03-06",
            updatedAtTimestamp="2026-03-06T12:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
        # day 3: date advances again, no new events (silent update)
        _entry(
            "2026-03-08T12:00:00Z",
            updatedAt="2026-03-07",
            updatedAtTimestamp="2026-03-07T08:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
    ]
    s = summarize_case(entries, today_iso="2026-04-18")
    assert s["silentUpdates"] == 2
    assert "sameDayRefreshes" not in s
    assert s["uscisUpdates"] == 3  # three distinct updatedAt dates
    # allEvents = timeline pill count: 1 IAF event on the latest snapshot +
    # 2 silent events detected across the captures.
    assert s["allEvents"] == 3
    assert s["daysPending"] == 57  # from 2026-02-20 → 2026-04-18
    assert s["stage"] == "Received"


def test_all_events_counts_events_plus_silent():
    # "All events" is the timeline pill count: every event row on the latest
    # snapshot plus every silent event. Three events on the latest snapshot
    # (even when two arrived in one capture) all show as pills → 3 + silent.
    entries = [
        _entry("2026-03-05T12:00:00Z", events=[{"eventCode": "IAF", "eventId": "a"}]),
        # one capture, two new events: both are distinct timeline pills
        _entry(
            "2026-04-01T12:00:00Z",
            updatedAt="2026-04-01",
            updatedAtTimestamp="2026-04-01T12:00:00Z",
            events=[
                {"eventCode": "IAF", "eventId": "a"},
                {"eventCode": "FTA0", "eventId": "b"},
                {"eventCode": "FTA0", "eventId": "c"},
            ],
        ),
    ]
    s = summarize_case(entries, today_iso="2026-04-18")
    silent = s["silentUpdates"]
    latest_events = len(entries[-1]["data"]["events"])
    assert s["allEvents"] == latest_events + silent
    assert s["allEvents"] == 3  # 3 events on the latest snapshot, 0 silent


def test_summarize_case_surfaces_upcoming_appointment_with_days_until():
    entry = _entry(
        "2026-04-15T12:00:00Z",
        notices=[
            {
                "actionType": "Appointment Scheduled",
                "letterId": "L1",
                "appointmentDateTime": "2026-04-20T13:00:00Z",
            },
        ],
    )
    s = summarize_case([entry], today_iso="2026-04-18")
    appt = s["upcomingAppointment"]
    assert appt is not None
    assert appt["letterId"] == "L1"
    assert appt["daysUntil"] == 2


# -------- branch coverage: classify_change ---------------------------------

def test_classify_change_notice_without_appointment():
    change = {
        "scalars": {},
        "events": {"added": [], "removed": []},
        "notices": {
            "added": [{"actionType": "RFE", "letterId": "L7"}],
            "removed": [],
        },
    }
    assert classify_change(change) == "notice"


def test_classify_change_status_fallback_for_non_timestamp_scalar():
    # A tracked scalar changed but not a decision flag nor timestamp-only.
    change = {
        "scalars": {"statusTitle": {"from": "Received", "to": "Processing"}},
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
    }
    assert classify_change(change) == "status"


def test_classify_change_appointment_wins_over_notice_when_removed():
    change = {
        "scalars": {},
        "events": {"added": [], "removed": []},
        "notices": {
            "added": [],
            "removed": [
                {"actionType": "Appointment", "appointmentDateTime": "2026-04-01T13:00:00Z"}
            ],
        },
    }
    assert classify_change(change) == "appointment"


# -------- snapshot_changes: collection-only diff path ---------------------

def test_snapshot_changes_detects_collection_only_diff():
    # No scalar change, but a document was added. Should count as a diff.
    from diff_utils import snapshot_changes
    e0 = _entry("2026-03-09T00:00:00Z", documents=[])
    e1 = _entry("2026-03-10T00:00:00Z", documents=[{"id": "doc1"}])
    changes = snapshot_changes([e0, e1])
    assert len(changes) == 1


# -------- stage_progression edge cases ------------------------------------

def test_stage_progression_with_divergence_overrides_current():
    prog = stage_progression([{"eventCode": "FTA0"}, {"eventCode": "RFE0"}])
    assert prog["current"] == "Request for Evidence outstanding"
    assert prog["divergence"] == "Request for Evidence outstanding"
    # Happy-path stepper still reflects reached state on FTA0
    states = [s["state"] for s in prog["steps"]]
    assert "current" in states  # Biometrics step marked current


def test_stage_progression_no_events_pending_receipt():
    prog = stage_progression([])
    assert prog["current"] == "Pending receipt"
    assert prog["divergence"] is None
    assert all(s["state"] == "upcoming" for s in prog["steps"])


# -------- summarize_case edge cases ---------------------------------------

def test_summarize_case_empty_entries_has_none_fields():
    from diff_utils import summarize_case
    s = summarize_case([], today_iso="2026-04-18")
    assert s["daysPending"] is None
    assert s["daysSinceUpdate"] is None
    assert s["uscisUpdates"] == 0
    assert s["allEvents"] == 0
    assert s["lastActivity"] is None
    assert s["upcomingAppointment"] is None
    assert s["stage"] == "Pending receipt"


def test_summarize_case_skips_past_appointments():
    entry = _entry(
        "2026-04-15T12:00:00Z",
        notices=[
            {
                "actionType": "Appointment Scheduled",
                "letterId": "PAST",
                "appointmentDateTime": "2026-01-01T13:00:00Z",
            },
        ],
    )
    s = summarize_case([entry], today_iso="2026-04-18")
    assert s["upcomingAppointment"] is None


def test_summarize_case_surfaces_last_activity_with_real_update_date():
    entries = [
        _entry(
            "2026-03-05T12:00:00Z",
            updatedAt="2026-03-05",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
        _entry(
            "2026-03-08T12:00:00Z",
            updatedAt="2026-03-07",  # silent update: date advanced, no new event
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
    ]
    s = summarize_case(entries, today_iso="2026-04-18")
    assert s["lastActivity"]["realUpdateDate"] == "2026-03-07"
    assert s["lastActivity"]["detectedOn"] == "2026-03-08"
    assert s["lastActivity"]["kind"] == "silent_update"


def test_summarize_case_picks_nearest_future_appointment():
    entry = _entry(
        "2026-04-15T12:00:00Z",
        notices=[
            {"actionType": "A", "letterId": "FAR",
             "appointmentDateTime": "2026-05-20T13:00:00Z"},
            {"actionType": "A", "letterId": "NEAR",
             "appointmentDateTime": "2026-04-22T13:00:00Z"},
        ],
    )
    s = summarize_case([entry], today_iso="2026-04-18")
    assert s["upcomingAppointment"]["letterId"] == "NEAR"


# -------- _days_between invalid-date path ---------------------------------

def test_days_between_returns_none_on_invalid_date():
    from diff_utils import _days_between
    assert _days_between("not-a-date", "2026-04-18") is None
    assert _days_between(None, "2026-04-18") is None
    assert _days_between("2026-04-18", None) is None
    assert _days_between("", "") is None


# -------- bin_by_day skips entries without capturedAt ---------------------

def test_bin_by_day_skips_entries_with_empty_capturedAt():
    entries = [
        {"capturedAt": "", "data": {}},
        _entry("2026-03-10T00:00:00Z"),
    ]
    days = bin_by_day(entries)
    assert len(days) == 1


def test_key_notice_falls_back_to_actiontype_when_no_letter_id():
    # Notices without `letterId` are keyed by actionType@generationDate — so
    # adding a duplicate should not surface as a diff.
    n = {"actionType": "X", "generationDate": "2026-04-18"}
    e0 = _entry("2026-03-09T00:00:00Z", notices=[n])
    e1 = _entry("2026-03-10T00:00:00Z", notices=[dict(n)])
    from diff_utils import snapshot_changes
    assert snapshot_changes([e0, e1]) == []


# ======================================================================
# Location-API day changes
# ======================================================================

def _loc_entry(captured_at: str, payload):
    """payload is whatever goes under the outer `data` key (i.e. the raw
    envelope). `None` or a dict like `{"receipt_details": {...}}` are valid."""
    return {"capturedAt": captured_at, "data": {"data": payload}}


def test_location_snapshot_changes_empty_history_produces_no_diffs():
    from diff_utils import location_snapshot_changes
    assert location_snapshot_changes([]) == []
    assert location_snapshot_changes([_loc_entry("2026-04-22T00:00:00Z", None)]) == []


def test_location_snapshot_changes_null_to_null_is_silent():
    from diff_utils import location_snapshot_changes
    entries = [
        _loc_entry("2026-04-20T00:00:00Z", None),
        _loc_entry("2026-04-21T00:00:00Z", None),
    ]
    assert location_snapshot_changes(entries) == []


def test_location_snapshot_changes_null_to_populated_emits_assigned():
    from diff_utils import location_snapshot_changes
    entries = [
        _loc_entry("2026-04-20T00:00:00Z", None),
        _loc_entry("2026-04-21T00:00:00Z", {
            "receipt_details": {
                "form": "I-765", "location": "SCD", "subtype": "147-C9",
            },
        }),
    ]
    changes = location_snapshot_changes(entries)
    assert len(changes) == 1
    c = changes[0]
    assert c["kind"] == "location_assigned"
    assert c["source"] == "location"
    assert c["scalars"]["location"] == {"from": None, "to": "SCD"}
    assert c["scalars"]["subtype"] == {"from": None, "to": "147-C9"}


def test_location_snapshot_changes_populated_to_different_populated_emits_changed():
    from diff_utils import location_snapshot_changes
    entries = [
        _loc_entry("2026-04-20T00:00:00Z", {
            "receipt_details": {"form": "I-765", "location": "SCD", "subtype": "147-C9"},
        }),
        _loc_entry("2026-04-21T00:00:00Z", {
            "receipt_details": {"form": "I-765", "location": "NSC", "subtype": "147-C9"},
        }),
    ]
    changes = location_snapshot_changes(entries)
    assert len(changes) == 1
    assert changes[0]["kind"] == "location_changed"
    assert changes[0]["scalars"] == {"location": {"from": "SCD", "to": "NSC"}}


def test_location_snapshot_changes_populated_to_null_emits_cleared():
    from diff_utils import location_snapshot_changes
    entries = [
        _loc_entry("2026-04-20T00:00:00Z", {
            "receipt_details": {"form": "I-765", "location": "SCD"},
        }),
        _loc_entry("2026-04-21T00:00:00Z", None),
    ]
    changes = location_snapshot_changes(entries)
    assert len(changes) == 1
    assert changes[0]["kind"] == "location_cleared"
    assert changes[0]["source"] == "location"


def test_location_snapshot_changes_skips_same_payload_days():
    from diff_utils import location_snapshot_changes
    entries = [
        _loc_entry("2026-04-20T00:00:00Z", {
            "receipt_details": {"form": "I-765", "location": "SCD"},
        }),
        _loc_entry("2026-04-21T00:00:00Z", {
            "receipt_details": {"form": "I-765", "location": "SCD"},
        }),
    ]
    assert location_snapshot_changes(entries) == []


def test_snapshot_changes_case_source_tagged():
    from diff_utils import snapshot_changes
    e0 = _entry("2026-03-09T00:00:00Z")
    e1 = _entry("2026-03-10T00:00:00Z", closed=True)
    out = snapshot_changes([e0, e1])
    assert out and out[0]["source"] == "case"
