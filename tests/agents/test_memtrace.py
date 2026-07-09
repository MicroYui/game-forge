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


def test_transition_graph_counts_results_per_state_action():
    m = MemTrace()
    for _ in range(3):
        m.record(_step(state="a", action_kind="move", result="blocked", state_hash="h1"))
    m.record(_step(state="a", action_kind="move", result="ok", state_hash="h1"))
    key = m.action_key({"kind": "move"})
    assert m.transitions["h1"][key]["blocked"] == 3
    assert m.transitions["h1"][key]["ok"] == 1
    assert m.no_progress_count("h1", {"kind": "move"}) == 3


def test_no_progress_count_zero_for_unseen_state_action():
    m = MemTrace()
    assert m.no_progress_count("nope", {"kind": "x"}) == 0


def test_empty_state_hash_not_indexed_in_transitions():
    m = MemTrace()
    m.record(_step(state="a", result="ok"))  # no state_hash
    assert m.transitions == {}


def test_recall_ranks_structurally_similar_recent_first():
    m = MemTrace()
    m.record(_step(state="alpha beta", action_kind="a", result="ok", state_hash="h1", step_index=0))
    m.record(_step(state="zeta eta", action_kind="b", result="ok", state_hash="h2", step_index=1))
    m.record(_step(state="alpha beta gamma", action_kind="c", result="ok", state_hash="h3", step_index=2))
    top = m.recall("alpha beta", task=None, k=1)
    assert len(top) == 1
    assert top[0].state_abstract == "alpha beta gamma"  # highest jaccard × recency


def test_recall_suppresses_repeated_dead_ends():
    m = MemTrace()
    # 'move' at h1 stalls 4× → heavily down-weighted vs a single 'talk' that progressed
    for _ in range(4):
        m.record(_step(state="room", action_kind="move", result="blocked", state_hash="h1"))
    m.record(_step(state="room", action_kind="talk", result="ok", state_hash="h1"))
    top = m.recall("room", task=None, k=1)
    assert top[0].action["kind"] == "talk"


def test_recall_text_none_when_empty():
    assert MemTrace().recall_text("x", task=None) is None


def test_recall_is_deterministic():
    m = MemTrace()
    for i in range(6):
        m.record(_step(state=f"s {i%2}", action_kind="a", result="ok", state_hash=f"h{i}"))
    assert [e.step_index for e in m.recall("s 0", None, 3)] == [e.step_index for e in m.recall("s 0", None, 3)]


def test_recall_embedding_off_by_default_calls_no_embedder():
    # Default MemTrace() has embedder=None → embedding term is neutral 1.0 and
    # recall is zero-model. (Structural/recency/verdict fully decide.)
    m = MemTrace()  # embedder=None
    m.record(_step(state="a b", result="ok", state_hash="h1"))
    assert m.recall("a b", None, 1)[0].state_abstract == "a b"


class _CountingEmbedder:
    """Deterministic bag-of-chars embedder that counts calls (test-only)."""
    def __init__(self):
        self.calls = 0
    def embed(self, text):
        self.calls += 1
        import collections
        c = collections.Counter(text)
        return [float(c.get(ch, 0)) for ch in "abcdefghijklmnopqrstuvwxyz "]


def test_recall_embedder_called_and_cached():
    emb = _CountingEmbedder()
    m = MemTrace(embedder=emb)
    m.record(_step(state="alpha", result="ok", state_hash="h1"))
    m.record(_step(state="alpha", result="ok", state_hash="h2"))  # same text
    m.recall("query", None, 2)
    # query embedded once + the distinct episode text once (cached across the two
    # identical-text episodes) = 2, not 3.
    assert emb.calls == 2


def test_recall_embedding_term_reranks():
    # Two episodes tie on recency/structure vs the query, but one is embed-close
    # (shares chars) and one embed-far; the close one must rank first — proving
    # the embedding term participates in scoring.
    emb = _CountingEmbedder()
    m = MemTrace(embedder=emb)
    m.record(_step(state="zzz", action_kind="far", result="ok", state_hash="h1", step_index=0))
    m.record(_step(state="qry", action_kind="near", result="ok", state_hash="h2", step_index=1))
    top = m.recall("qry", None, 1)  # 'qry' shares all chars with the 'qry' episode
    assert top[0].action["kind"] == "near"


def test_add_and_lookup_skill():
    m = MemTrace()
    m.add_skill("nav_to_giver", "start#h1", "at#giver", [{"kind": "move"}, {"kind": "talk"}])
    hits = m.skills_for("start#h1", "at#giver")
    assert hits == [[{"kind": "move"}, {"kind": "talk"}]]
    assert m.skills_for("x", "y") == []


def test_reflect_writes_downweighting_episode():
    m = MemTrace()
    m.record(_step(state="room", action_kind="move", result="unreachable", state_hash="h1"))
    note = m.reflect(m.trace[:], verdict="unreachable")
    assert isinstance(note, str) and note
    assert any(e.verdict < 0 for e in m.trace)


class _RaisingRouter:
    def route(self, *a, **k):
        raise RuntimeError("no live call in test")


def test_deterministic_compactor_never_touches_router():
    # Default MemTrace uses DeterministicCompactor → zero-model even if a router
    # that would explode is handed in.
    m = MemTrace()  # compactor=DeterministicCompactor()
    for i in range(5):
        m.record(_step(state=f"s{i}", result="ok", state_hash=f"h{i}"))
    out = m.compact(m.trace[:], verdicts=[], router=_RaisingRouter())
    assert isinstance(out, str) and out  # produced a digest, no model call


def test_llm_compactor_uses_router_and_fails_closed():
    from gameforge.agents.playtest.memory import LLMCompactor
    m = MemTrace(compactor=LLMCompactor())
    for i in range(5):
        m.record(_step(state=f"s{i}", result="ok", state_hash=f"h{i}"))
    # A raising router must NOT crash the run — LLMCompactor degrades to the
    # deterministic digest (fail-closed).
    out = m.compact(m.trace[:], verdicts=[], router=_RaisingRouter())
    assert isinstance(out, str) and out
