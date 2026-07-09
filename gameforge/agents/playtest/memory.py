"""MemTrace (M2b-2): from-scratch long-horizon memory for the Playtest Agent.

Design borrowed (not dependencies): TITAN (down-weight repeated no-progress) +
Voyager (reusable skill layer) + Generative-Agents (recency×relevance×verdict
recall) + HiAgent/AWM/MemGPT (compaction at task-step boundaries). Recall is
DETERMINISTIC arithmetic — no model call — so ranking is fully reproducible and
unit-testable. Only `compact` touches the Router (replayable), and only when a
MemTrace instance is actually attached (`memory is not None`); with no memory
the Playtest loop is byte-identical to M2b-1.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot
from gameforge.runtime.model_router.router import ModelRouter

# Results that mean the state/action pair advanced nothing (TITAN down-weight).
_NO_PROGRESS_RESULTS = frozenset({"blocked", "unreachable", "noop", "invalid", "already"})

# No-progress occurrences beyond this many stop adding further down-weight.
_NO_PROGRESS_CAP = 4

# Embedding-similarity remap range: cosine in [-1, 1] -> [_EMBED_FLOOR, 1.0].
# Never fully zeroes the term (a poor embedding match still leaves the other
# three deterministic factors in control) — mirrors verdict_weight's floor.
_EMBED_FLOOR = 0.05

# reflect() verdicts that write a NEGATIVE down-weighting episode (TITAN-style);
# any other verdict string writes a neutral (verdict=0.0) note.
_NEGATIVE_REFLECT_VERDICTS = frozenset({"unreachable", "abort_quest", "stuck", "fail"})

# How many trailing episodes DeterministicCompactor includes in its tail digest.
_COMPACT_TAIL = 8

_COMPACT_SNAPSHOT = ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag="m2a@1")
_COMPACT_PROMPT_VERSION = "playtest.memory.compact@1"
_COMPACT_NODE_ID = "playtest.memory"


def _action_key(action: dict) -> str:
    kind = str(action.get("kind", action.get("type", "?")))
    target = action.get("target") or action.get("target_id") or ""
    return f"{kind}:{target}"


def _episode_fields(item: "Episode | dict") -> tuple[str, dict, str, str]:
    """Normalize a trace item (Episode or the raw `step` dict) to its fields."""
    if isinstance(item, Episode):
        return item.state_abstract, dict(item.action), item.result, item.state_hash
    return (
        str(item.get("state", item.get("state_abstract", ""))),
        dict(item.get("action", {})),
        str(item.get("result", "")),
        str(item.get("state_hash", "")),
    )


class Embedder(Protocol):
    """Router-backed text embedder, injected by the caller (default: none).

    Ships as a protocol only — no concrete implementation is wired into CI.
    When absent (`MemTrace(embedder=None)`, the default), `recall`'s embedding
    term is the neutral 1.0 and recall is fully deterministic + zero-model.
    """

    def embed(self, text: str) -> list[float]: ...


def _tokens(text: str) -> set[str]:
    return set(text.split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return dot / (nu * nv)


def _remap_cosine(cos: float) -> float:
    return _EMBED_FLOOR + (cos + 1.0) / 2.0 * (1.0 - _EMBED_FLOOR)


@dataclass(frozen=True)
class Episode:
    state_abstract: str
    action: dict
    result: str
    state_hash: str = ""
    tick: int = -1
    step_index: int = 0
    verdict: float = 0.0  # verifier-conditioned weight delta (reflect/compact set it)


class Compactor(Protocol):
    """Compaction strategy: summarize a completed trace into a short digest.

    Two first-class strategies ship (Task 8b compares them empirically):
    `DeterministicCompactor` (zero-model) and `LLMCompactor` (router-backed,
    fail-closed to the deterministic digest). Both must never raise.
    """

    def compact(
        self,
        trace: list[Episode],
        verdicts: list,
        *,
        router: ModelRouter | None = None,
        node_id: str = _COMPACT_NODE_ID,
    ) -> str: ...


class DeterministicCompactor:
    """Zero-model compaction: verified-skill highlights + a tail digest.

    Never touches `router` — this is the strategy that keeps the whole memory
    layer model-free, used as the default and as every fail-closed fallback.
    """

    def compact(
        self,
        trace: list[Episode],
        verdicts: list,
        *,
        router: ModelRouter | None = None,
        node_id: str = _COMPACT_NODE_ID,
    ) -> str:
        del router, node_id  # zero-model by construction: never referenced
        if not trace:
            return "MemTrace compaction (deterministic): empty trace."
        verified = [ep for ep in trace if ep.verdict > 0]
        tail = trace[-_COMPACT_TAIL:]
        lines = [
            f"MemTrace compaction (deterministic): {len(trace)} step(s), "
            f"{len(verified)} verified, {len(list(verdicts))} verdict(s)."
        ]
        if verified:
            lines.append("Verified:")
            lines.extend(
                f"  - {ep.state_abstract[:40]} :: {_action_key(ep.action)} -> {ep.result}"
                for ep in verified
            )
        lines.append("Tail:")
        lines.extend(
            f"  - {ep.state_abstract[:40]} :: {_action_key(ep.action)} -> {ep.result}" for ep in tail
        )
        return "\n".join(lines)


class MemTrace:
    def __init__(
        self,
        embedder: Embedder | None = None,
        compactor: Compactor | None = None,
    ) -> None:
        self.trace: list[Episode] = []
        # state_hash -> action_key -> Counter[result] (persistent transition graph).
        self.transitions: dict[str, dict[str, Counter]] = {}
        # (start_sig, goal_sig) -> list of {"name", "actions"} verified sub-trajectories.
        self.skills: dict[tuple[str, str], list[dict]] = {}
        self._embedder = embedder
        self._embed_cache: dict[str, list[float]] = {}
        self._compactor: Compactor = compactor if compactor is not None else DeterministicCompactor()
        # Set by `compact(...)`; once populated, `recall_text` prepends it to
        # every subsequent recall so the compactor CHOICE causally changes
        # what the planner/executor see (Task 8b needs Det vs LLM to diverge).
        self._compacted_digest: str | None = None

    def _append(
        self,
        *,
        state_abstract: str,
        action: dict,
        result: str,
        state_hash: str,
        tick: int,
        verdict: float = 0.0,
        step_index: int | None = None,
    ) -> Episode:
        ep = Episode(
            state_abstract=state_abstract,
            action=action,
            result=result,
            state_hash=state_hash,
            tick=tick,
            step_index=step_index if step_index is not None else len(self.trace),
            verdict=verdict,
        )
        self.trace.append(ep)
        if state_hash:
            bucket = self.transitions.setdefault(state_hash, {})
            bucket.setdefault(_action_key(action), Counter())[result] += 1
        return ep

    def record(self, step: dict) -> None:
        self._append(
            state_abstract=str(step["state"]),
            action=dict(step["action"]),
            result=str(step["result"]),
            state_hash=str(step.get("state_hash", "")),
            tick=int(step.get("tick", -1)),
            step_index=int(step["step_index"]) if "step_index" in step else None,
        )

    @staticmethod
    def action_key(action: dict) -> str:
        return _action_key(action)

    def no_progress_count(self, state_hash: str, action: dict) -> int:
        bucket = self.transitions.get(state_hash, {}).get(self.action_key(action))
        if bucket is None:
            return 0
        return sum(n for r, n in bucket.items() if r in _NO_PROGRESS_RESULTS)

    def _embed_cached(self, text: str) -> list[float]:
        assert self._embedder is not None
        cached = self._embed_cache.get(text)
        if cached is None:
            cached = self._embedder.embed(text)
            self._embed_cache[text] = cached
        return cached

    def _embedding_similarity(self, query_text: str, other_text: str) -> float:
        if self._embedder is None:
            return 1.0  # neutral term: fully deterministic, zero-model default
        return _remap_cosine(_cosine(self._embed_cached(query_text), self._embed_cached(other_text)))

    def recall(self, state: str, task: object, k: int = 3) -> list[Episode]:
        if not self.trace:
            return []
        n = len(self.trace)
        query_tokens = _tokens(state)
        scored: list[tuple[float, int, int, Episode]] = []
        for i, ep in enumerate(self.trace):
            age = (n - 1) - ep.step_index
            recency = 1.0 / (1.0 + age)
            structural = _jaccard(query_tokens, _tokens(ep.state_abstract))
            no_progress = self.no_progress_count(ep.state_hash, ep.action)
            verdict_weight = max(1.0 + ep.verdict - 0.5 * min(no_progress, _NO_PROGRESS_CAP), 0.05)
            embedding_sim = self._embedding_similarity(state, ep.state_abstract)
            score = recency * structural * verdict_weight * embedding_sim
            scored.append((score, ep.step_index, i, ep))
        # Highest score first; ties -> later step_index first, then later insertion.
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2]))
        return [t[3] for t in scored[:k]]

    def recall_text(self, state: str, task: object, k: int = 3) -> str | None:
        episodes = self.recall(state, task, k)
        lines = [
            f"- at {ep.state_abstract[:60]} did {self.action_key(ep.action)} -> {ep.result}"
            for ep in episodes
        ]
        body = "\n".join(lines) if lines else None
        if self._compacted_digest is None:
            # Unchanged path (no compaction has run yet): byte-identical to
            # before this fix, including the empty-trace `None` case.
            return body
        summary = f"Summary of progress so far:\n{self._compacted_digest}"
        if body is None:
            return summary
        return f"{summary}\n\n{body}"

    # -- Skill layer (Voyager): exact-match reusable verified sub-trajectories --

    def add_skill(self, name: str, start_sig: str, goal_sig: str, actions: list[dict]) -> None:
        key = (start_sig, goal_sig)
        self.skills.setdefault(key, []).append({"name": name, "actions": [dict(a) for a in actions]})

    def skills_for(self, start_sig: str, goal_sig: str) -> list[list[dict]]:
        return [list(rec["actions"]) for rec in self.skills.get((start_sig, goal_sig), [])]

    # -- Reflection: deterministic verdict-conditioned down-weighting write --

    def reflect(self, failed_trace: list[dict] | list[Episode], verdict: str) -> str:
        verdict_value = -1.0 if verdict in _NEGATIVE_REFLECT_VERDICTS else 0.0
        if failed_trace:
            state_abstract, action, result, state_hash = _episode_fields(failed_trace[-1])
        else:
            state_abstract, action, result, state_hash = "", {}, "", ""
        # Reuse the ORIGINAL result (not a synthesized one) so this note also
        # reinforces the transition graph's no-progress count for that exact
        # (state_hash, action) pair — this is how recall down-weights the path,
        # not merely this one new episode's own (negative) verdict.
        self._append(
            state_abstract=state_abstract,
            action=action,
            result=result or verdict,
            state_hash=state_hash,
            tick=-1,
            verdict=verdict_value,
        )
        return (
            f"reflect[{verdict}]: at '{state_abstract[:60]}' "
            f"action={_action_key(action)} result={result} verdict={verdict_value:+.1f}"
        )

    # -- Compaction: dispatches to the configured strategy (both fail-closed) --

    def compact(
        self,
        trace: list[Episode],
        verdicts: list,
        router: ModelRouter | None = None,
        node_id: str = _COMPACT_NODE_ID,
    ) -> str:
        digest = self._compactor.compact(trace, verdicts, router=router, node_id=node_id)
        # STORE it so `recall_text` surfaces it on subsequent steps — this is
        # what makes compaction causally active instead of a measured no-op.
        self._compacted_digest = digest
        return digest


class LLMCompactor:
    """Router-backed tail summarizer (recorded/replayable through the Router).

    Fails closed to `DeterministicCompactor`'s digest on ANY transport/parse
    failure — construction, request-building, the call itself, or an empty
    response all degrade to the deterministic digest rather than raise.
    """

    def __init__(self) -> None:
        self._fallback = DeterministicCompactor()

    def compact(
        self,
        trace: list[Episode],
        verdicts: list,
        *,
        router: ModelRouter | None = None,
        node_id: str = _COMPACT_NODE_ID,
    ) -> str:
        digest = self._fallback.compact(trace, verdicts, router=None, node_id=node_id)
        if router is None:
            return digest
        try:
            req = ModelRequest(
                model_snapshot=_COMPACT_SNAPSHOT,
                messages=[Message(role="user", content=digest)],
                params={"max_tokens": 512, "temperature": 0},
                agent_node_id=node_id,
                prompt_version=_COMPACT_PROMPT_VERSION,
            )
            resp = router.call(req)
            text = str(resp.response_normalized).strip()
            if not text:
                raise ValueError("empty compaction response")
            return text
        except Exception:
            return digest
