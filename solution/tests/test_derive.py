from pipeline.derive import Call, effective_disposition, lead_bucket

TERMINAL = {"renewed", "dnd", "not_interested"}
is_terminal = lambda code: code in TERMINAL


def call(cid="c1", sets=(), overrides=(), failed=False, connected=False):
    c = Call(cid, 1, initiated="2026-10-01T00:00:00.000000", telephony_failed=failed,
             connected=connected)
    c.sets = list(sets)
    c.overrides = list(overrides)
    return c


def test_effective_none_when_no_disposition():
    assert effective_disposition(call()) is None


def test_effective_latest_set_wins():
    c = call(sets=[("2026-10-01T10:00", "e2", "callback_requested"),
                   ("2026-10-01T09:00", "e1", "renewed")])
    assert effective_disposition(c) == "callback_requested"


def test_override_beats_even_a_later_set():
    c = call(sets=[("2026-10-01T12:00", "e9", "renewed")],
             overrides=[("2026-10-01T10:00", "e2", "not_interested")])
    assert effective_disposition(c) == "not_interested"


def test_latest_override_wins():
    c = call(overrides=[("2026-10-02T10:00", "e5", "dnd"), ("2026-10-03T10:00", "e6", "renewed")])
    assert effective_disposition(c) == "renewed"


def test_bucket_yet_to_reach_out():
    assert lead_bucket([], is_terminal) == "yet_to_reach_out"


def test_bucket_completed_via_override_only():
    c = call(sets=[("t1", "e1", "callback_requested")], overrides=[("t2", "e2", "dnd")])
    assert lead_bucket([c], is_terminal) == "completed"


def test_override_can_undo_terminal():
    c = call(sets=[("t1", "e1", "renewed")], overrides=[("t2", "e2", "callback_requested")])
    assert lead_bucket([c], is_terminal) == "in_progress_dialed"


def test_bucket_completed_beats_telephony_failed():
    done = call("a", sets=[("t1", "e1", "renewed")], failed=True)
    assert lead_bucket([done], is_terminal) == "completed"


def test_bucket_all_failed():
    assert lead_bucket([call("a", failed=True), call("b", failed=True)],
                       is_terminal) == "in_progress_telephony_failed"


def test_bucket_one_failed_one_not_is_dialed():
    assert lead_bucket([call("a", failed=True), call("b")], is_terminal) == "in_progress_dialed"
