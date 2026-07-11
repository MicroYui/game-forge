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
    # Discriminating by construction: 'talk' progressed but is OLDER, while
    # 'move' stalls 4x at the SAME state_hash and is the MOST RECENT run of
    # episodes. Recency alone would hand the win to the last 'move' (age=0,
    # recency=1.0 > talk's recency=0.2). The only way 'talk' can still win is
    # TITAN's no_progress_count down-weight crushing 'move's verdict_weight to
    # its floor (0.05). If that down-weight is deleted, 'move' wins instead.
    m = MemTrace()
    m.record(_step(state="room", action_kind="talk", result="ok", state_hash="h1"))
    for _ in range(4):
        m.record(_step(state="room", action_kind="move", result="blocked", state_hash="h1"))
    top = m.recall("room", task=None, k=1)
    assert top[0].action["kind"] == "talk"


def test_recall_text_none_when_empty():
    assert MemTrace().recall_text("x", task=None) is None


def test_recall_is_deterministic():
    m = MemTrace()
    for i in range(6):
        m.record(_step(state=f"s {i%2}", action_kind="a", result="ok", state_hash=f"h{i}"))
    first = [e.step_index for e in m.recall("s 0", None, 3)]
    second = [e.step_index for e in m.recall("s 0", None, 3)]
    # Non-vacuous: guard against a broken recall that trivially returns `[]`
    # (or fewer than k) both times, which would satisfy equality without
    # proving anything about the ranking itself.
    assert len(first) == 3
    assert len(second) == 3
    # The full ranked order (not just set membership) must be byte-identical
    # across repeated calls against the same trace.
    assert first == second


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
    # Discriminating by construction, not accident. Query "p q" vs:
    #   near = "p pq"  -> tokens {"p","pq"}, jaccard = 1/3
    #   far  = "q fz"  -> tokens {"q","fz"}, jaccard = 1/3   (EQUAL structural term)
    # Both use result="ok" (never TITAN-down-weighted) and an explicit, IDENTICAL
    # step_index=0 (n=2 episodes -> EQUAL recency too). So structural, recency and
    # verdict_weight are tied exactly; only the embedding term can separate them.
    # _CountingEmbedder's bag-of-chars gives cosine(query, near) = 0.943 (shares
    # 'p' and repeats it) vs cosine(query, far) = 0.577 (shares only 'q') --
    # verified numerically, not assumed. So 'near' must win under the real
    # embedding term.
    emb = _CountingEmbedder()
    m = MemTrace(embedder=emb)
    m.record(_step(state="p pq", action_kind="near", result="ok", state_hash="h1", step_index=0))
    m.record(_step(state="q fz", action_kind="far", result="ok", state_hash="h2", step_index=0))
    top = m.recall("p q", task=None, k=1)
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
    """Stub matching the REAL `ModelRouter.call(req)` API (not a decoy method
    name) so a raising router actually exercises LLMCompactor's call site.
    Counts invocations so tests can prove the router path was truly entered.
    """
    def __init__(self):
        self.calls = 0

    def call(self, req):
        self.calls += 1
        raise RuntimeError("no live call in test")


def test_deterministic_compactor_never_touches_router():
    # Default MemTrace uses DeterministicCompactor → zero-model even if a router
    # that would explode is handed in.
    m = MemTrace()  # compactor=DeterministicCompactor()
    for i in range(5):
        m.record(_step(state=f"s{i}", result="ok", state_hash=f"h{i}"))
    router = _RaisingRouter()
    out = m.compact(m.trace[:], verdicts=[], router=router)
    assert isinstance(out, str) and out  # produced a digest, no model call
    assert router.calls == 0  # proves DeterministicCompactor never dials out


def test_llm_compactor_uses_router_and_fails_closed():
    from gameforge.agents.playtest.memory import LLMCompactor
    m = MemTrace(compactor=LLMCompactor())
    for i in range(5):
        m.record(_step(state=f"s{i}", result="ok", state_hash=f"h{i}"))
    # A raising router must NOT crash the run — LLMCompactor degrades to the
    # deterministic digest (fail-closed) -- but only after genuinely calling
    # router.call(req) and catching its failure (not e.g. an AttributeError
    # on a mismatched stub method name, which would fail-closed for the wrong
    # reason and pass even if the router were never invoked).
    router = _RaisingRouter()
    out = m.compact(m.trace[:], verdicts=[], router=router)
    assert isinstance(out, str) and out
    assert router.calls == 1  # the LLM path was actually entered


def test_deterministic_compactor_digest_reflects_trace():
    from gameforge.agents.playtest.memory import DeterministicCompactor
    trace = [
        Episode(
            state_abstract="alpha room",
            action={"kind": "talk", "target": "npc"},
            result="quest_given",
            state_hash="h1",
            tick=0,
            step_index=0,
            verdict=1.0,  # verified
        ),
        Episode(
            state_abstract="beta room",
            action={"kind": "move"},
            result="ok",
            state_hash="h2",
            tick=1,
            step_index=1,
            verdict=0.0,
        ),
    ]
    out = DeterministicCompactor().compact(trace, verdicts=["ok", "ok"])
    assert isinstance(out, str) and out
    # Reflects real trace content, not a canned/empty string.
    assert "2 step(s)" in out
    assert "1 verified" in out
    assert "talk:npc" in out  # the verified episode's action key appears


def test_deterministic_compactor_empty_trace_never_raises():
    from gameforge.agents.playtest.memory import DeterministicCompactor
    out = DeterministicCompactor().compact([], verdicts=[])
    assert isinstance(out, str) and out


# ---------------------------------------------------------------------------
# Compaction must be causally active: `compact(...)` STORES its digest, and
# `recall_text` surfaces it on subsequent calls. Before this wiring the digest
# was computed and thrown away — DeterministicCompactor vs LLMCompactor could
# never produce a different `recall_text`, making Task 8b's "compare the two
# compactors' effect on completion rate" undiscriminating by construction.
# ---------------------------------------------------------------------------

class _ScriptedRouter:
    """Fake router (no transport, no live call): always answers a fixed,
    distinct summary string, and counts invocations so a test can prove the
    router path was genuinely entered."""

    def __init__(self, text: str, default_model_snapshot=None) -> None:
        self._text = text
        self.calls = 0
        self.default_model_snapshot = default_model_snapshot
        self.requests = []

    def call(self, req):  # noqa: ANN001 — Protocol shape only
        self.calls += 1
        self.requests.append(req)
        from gameforge.contracts.model_router import ModelResponse
        return ModelResponse(response_normalized=self._text)


def test_llm_compactor_uses_router_model_policy_and_allows_node_override():
    from gameforge.agents.base import DEFAULT_SNAPSHOT, M2_REPLAY_SNAPSHOT
    from gameforge.agents.playtest.memory import LLMCompactor

    step = _step(state="a", result="ok", state_hash="h1")
    live_router = _ScriptedRouter("live", default_model_snapshot=DEFAULT_SNAPSHOT)
    replay_router = _ScriptedRouter("replay", default_model_snapshot=DEFAULT_SNAPSHOT)

    live = MemTrace(compactor=LLMCompactor())
    live.record(step)
    live.compact(live.trace[:], verdicts=[], router=live_router)

    replay = MemTrace(compactor=LLMCompactor(snapshot=M2_REPLAY_SNAPSHOT))
    replay.record(step)
    replay.compact(replay.trace[:], verdicts=[], router=replay_router)

    assert live_router.requests[0].model_snapshot == DEFAULT_SNAPSHOT
    assert live_router.requests[0].params == {"max_tokens": 512}
    assert replay_router.requests[0].model_snapshot == M2_REPLAY_SNAPSHOT
    assert replay_router.requests[0].params == {
        "max_tokens": 512,
        "temperature": 0,
    }


def test_recall_text_gains_digest_section_only_after_compact():
    m = MemTrace()  # DeterministicCompactor by default
    m.record(_step(state="a", result="ok", state_hash="h1"))

    before = m.recall_text("a", task=None)
    assert before is not None
    assert "Summary of progress so far:" not in before  # no compaction ran yet

    digest = m.compact(m.trace[:], verdicts=[])
    after = m.recall_text("a", task=None)
    assert after is not None
    assert after.startswith(f"Summary of progress so far:\n{digest}\n\n")
    assert after.endswith(before)  # recall-item lines still present, unchanged


def test_compactor_choice_changes_injected_recall_text():
    # Same trace, same query — only the compactor differs. If the digest were
    # computed and discarded (the pre-fix bug), det_text == llm_text always.
    step = _step(state="a", result="ok", state_hash="h1")

    det = MemTrace()  # DeterministicCompactor
    det.record(step)
    det.compact(det.trace[:], verdicts=[])
    det_text = det.recall_text("a", task=None)

    from gameforge.agents.playtest.memory import LLMCompactor
    router = _ScriptedRouter("LLM DISTINCT SUMMARY: quest nearly complete")
    llm = MemTrace(compactor=LLMCompactor())
    llm.record(step)
    llm.compact(llm.trace[:], verdicts=[], router=router)
    llm_text = llm.recall_text("a", task=None)

    assert router.calls == 1  # the LLM path was genuinely entered, not skipped
    assert det_text != llm_text  # the compactor CHOICE changed the injected context
    assert "LLM DISTINCT SUMMARY" in llm_text
    assert "LLM DISTINCT SUMMARY" not in det_text


def test_recall_text_returns_summary_only_when_digest_set_but_no_recall_items():
    m = MemTrace()
    digest = m.compact([], verdicts=[])  # empty trace -> deterministic empty-trace digest
    assert m.trace == []
    assert m.recall_text("anything", task=None) == f"Summary of progress so far:\n{digest}"


def test_recall_text_none_when_truly_empty_and_uncompacted():
    # No trace, no compact() call yet -> still None, unchanged from before this fix.
    assert MemTrace().recall_text("x", task=None) is None
