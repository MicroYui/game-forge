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

# Results that mean the state/action pair advanced nothing (TITAN down-weight).
_NO_PROGRESS_RESULTS = frozenset({"blocked", "unreachable", "noop", "invalid", "already"})

# No-progress occurrences beyond this many stop adding further down-weight.
_NO_PROGRESS_CAP = 4

# Embedding-similarity remap range: cosine in [-1, 1] -> [_EMBED_FLOOR, 1.0].
# Never fully zeroes the term (a poor embedding match still leaves the other
# three deterministic factors in control) — mirrors verdict_weight's floor.
_EMBED_FLOOR = 0.05


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


class MemTrace:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self.trace: list[Episode] = []
        # state_hash -> action_key -> Counter[result] (persistent transition graph).
        self.transitions: dict[str, dict[str, Counter]] = {}
        self._embedder = embedder
        self._embed_cache: dict[str, list[float]] = {}

    def record(self, step: dict) -> None:
        action_dict = dict(step["action"])
        state_hash = str(step.get("state_hash", ""))
        result = str(step["result"])
        self.trace.append(
            Episode(
                state_abstract=str(step["state"]),
                action=action_dict,
                result=result,
                state_hash=state_hash,
                tick=int(step.get("tick", -1)),
                step_index=int(step.get("step_index", len(self.trace))),
            )
        )
        if state_hash:
            bucket = self.transitions.setdefault(state_hash, {})
            bucket.setdefault(self.action_key(action_dict), Counter())[result] += 1

    @staticmethod
    def action_key(action: dict) -> str:
        kind = str(action.get("kind", action.get("type", "?")))
        target = action.get("target") or action.get("target_id") or ""
        return f"{kind}:{target}"

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
        if not episodes:
            return None
        lines = [
            f"- at {ep.state_abstract[:60]} did {self.action_key(ep.action)} -> {ep.result}"
            for ep in episodes
        ]
        return "\n".join(lines)
