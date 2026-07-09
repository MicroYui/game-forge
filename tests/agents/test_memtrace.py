from gameforge.agents.playtest.memory import Episode, MemTrace


def _step(state="s0", action_kind="move", result="ok", **kw):
    return {"state": state, "action": {"kind": action_kind}, "result": result, **kw}


def test_record_appends_episode_in_order():
    m = MemTrace()
    m.record(_step(state="a", result="ok", state_hash="h1", tick=0))
    m.record(_step(state="b", result="blocked", state_hash="h2", tick=1))
    assert len(m.trace) == 2
    assert isinstance(m.trace[0], Episode)
    assert m.trace[0].state_abstract == "a"
    assert m.trace[0].result == "ok"
    assert m.trace[0].step_index == 0
    assert m.trace[1].step_index == 1
    assert m.trace[1].state_hash == "h2"


def test_record_defaults_missing_optional_keys():
    m = MemTrace()
    m.record({"state": "x", "action": {"kind": "talk"}, "result": "ok"})
    e = m.trace[0]
    assert e.state_hash == ""
    assert e.tick == -1
    assert e.step_index == 0
