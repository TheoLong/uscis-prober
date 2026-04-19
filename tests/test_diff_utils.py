"""Unit tests for diff_utils: pure functions, no I/O."""

from diff_utils import (
    CANONICAL_STAGES,
    bin_by_day,
    classify_change,
    compute_change,
    day_changes,
    infer_stage,
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


def test_classify_change_silent_ping_when_only_timestamp_moves():
    change = {
        "scalars": {
            "updatedAtTimestamp": {"from": "2026-03-10T14:59:52Z",
                                   "to": "2026-03-10T16:59:58Z"},
        },
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
    }
    assert classify_change(change) == "same_day_refresh"


# -------- compute_change + day_changes ------------------------------------

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


def test_day_changes_filters_out_unchanged_day_pairs():
    entries = [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z"),  # identical -> no diff
        _entry("2026-03-11T00:00:00Z", closed=True),  # decision flip
    ]
    changes = day_changes(entries)
    assert len(changes) == 1
    assert changes[0]["kind"] == "decision"


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

def test_summarize_case_counts_silent_updates_and_same_day_refreshes_separately():
    entries = [
        # day 1: received
        _entry(
            "2026-03-05T12:00:00Z",
            updatedAt="2026-03-05",
            updatedAtTimestamp="2026-03-05T10:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
        # day 2: same-day re-stamp (ping)
        _entry(
            "2026-03-06T12:00:00Z",
            updatedAt="2026-03-05",
            updatedAtTimestamp="2026-03-05T12:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
        # day 3: date advanced, no new events (silent update)
        _entry(
            "2026-03-08T12:00:00Z",
            updatedAt="2026-03-07",
            updatedAtTimestamp="2026-03-07T08:00:00Z",
            events=[{"eventCode": "IAF", "eventId": "a"}],
        ),
    ]
    s = summarize_case(entries, today_iso="2026-04-18")
    assert s["silentUpdates"] == 1
    assert s["sameDayRefreshes"] == 1
    assert s["uscisUpdates"] == 2  # two distinct updatedAt dates
    # allUpdates = events on the latest snapshot + silent-update diffs.
    # Latest snapshot has 1 IAF event; 1 silent_update diff detected.
    assert s["allUpdates"] == 2
    assert s["daysPending"] == 57  # from 2026-02-20 → 2026-04-18
    assert s["stage"] == "Received"


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


def test_classify_change_silent_ping_only_timestamp_changes():
    change = {
        "scalars": {"updatedAtTimestamp": {"from": "a", "to": "b"}},
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
        "documents": {"added": [], "removed": []},
    }
    assert classify_change(change) == "same_day_refresh"


# -------- day_changes: collection-only diff path --------------------------

def test_day_changes_detects_collection_only_diff():
    # No scalar change, but a document was added. Should count as a diff.
    from diff_utils import day_changes
    e0 = _entry("2026-03-09T00:00:00Z", documents=[])
    e1 = _entry("2026-03-10T00:00:00Z", documents=[{"id": "doc1"}])
    changes = day_changes([e0, e1])
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
    assert s["allUpdates"] == 0
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
    from diff_utils import day_changes
    assert day_changes([e0, e1]) == []
