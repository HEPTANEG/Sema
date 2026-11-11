import hashlib
import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass


DEFAULT_WINDOW_SIZE = 10
DEFAULT_THRESHOLDS = (3, 5, 8)
DEFAULT_MINHASH_PERMUTATIONS = 32
DEFAULT_MINHASH_SIMILARITY = 0.8
_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _normalize_string(value):
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return " ".join(normalized.split())


def _canonical(value):
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", repr(value)]
    if isinstance(value, str):
        return ["string", _normalize_string(value)]
    if isinstance(value, list):
        return ["array", [_canonical(item) for item in value]]
    if isinstance(value, dict):
        return [
            "object",
            [[_normalize_string(str(key)), _canonical(item)] for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))],
        ]
    return [type(value).__name__, _normalize_string(str(value))]


def canonicalize_model_output(model_output):
    return json.dumps(_canonical(model_output), ensure_ascii=False, separators=(",", ":"))


def _token_shingles(canonical_text, size=3):
    tokens = _TOKEN_PATTERN.findall(canonical_text)
    if len(tokens) <= size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[index:index + size]) for index in range(len(tokens) - size + 1)}


def _minhash(canonical_text, permutations):
    shingles = _token_shingles(canonical_text)
    if not shingles:
        return (0,) * permutations
    return tuple(
        min(
            int.from_bytes(hashlib.blake2b(f"{seed}:{shingle}".encode("utf-8"), digest_size=8).digest(), "big")
            for shingle in shingles
        )
        for seed in range(permutations)
    )


def _minhash_similarity(left, right):
    if not left and not right:
        return 1.0
    return sum(left_value == right_value for left_value, right_value in zip(left, right)) / max(len(left), 1)


@dataclass(frozen=True)
class RepetitionSignal:
    threshold: int
    repeated_rounds: int
    pattern_type: str
    similarity: dict
    matched_rounds: tuple
    model_round: int
    stage: str | None
    tool_names: tuple
    tool_span_ids: tuple

    def as_dict(self):
        return {
            "threshold": self.threshold,
            "repeated_rounds": self.repeated_rounds,
            "pattern_type": self.pattern_type,
            "similarity": dict(self.similarity),
            "matched_rounds": list(self.matched_rounds),
            "model_round": self.model_round,
            "stage": self.stage,
            "tool_names": list(self.tool_names),
            "tool_span_ids": list(self.tool_span_ids),
        }


@dataclass(frozen=True)
class _Round:
    model_round: int
    stage: str | None
    canonical: str
    exact_hash: str
    minhash: tuple
    tool_names: tuple
    tool_span_ids: tuple


class ContextRepetitionDetector:
    def __init__(self, window_size=DEFAULT_WINDOW_SIZE, thresholds=DEFAULT_THRESHOLDS, minhash_similarity=DEFAULT_MINHASH_SIMILARITY, minhash_permutations=DEFAULT_MINHASH_PERMUTATIONS):
        if int(window_size) < 1:
            raise ValueError("window_size must be positive")
        normalized_thresholds = tuple(sorted({int(value) for value in thresholds}))
        if not normalized_thresholds or normalized_thresholds[0] < 1:
            raise ValueError("thresholds must contain positive integers")
        if normalized_thresholds[-1] > int(window_size):
            raise ValueError("thresholds must not exceed window_size")
        if not 0 <= float(minhash_similarity) <= 1:
            raise ValueError("minhash_similarity must be between 0 and 1")
        if int(minhash_permutations) < 1:
            raise ValueError("minhash_permutations must be positive")
        self.window_size = int(window_size)
        self.thresholds = normalized_thresholds
        self.minhash_similarity = float(minhash_similarity)
        self.minhash_permutations = int(minhash_permutations)
        self._window = deque(maxlen=self.window_size)
        self._cluster = []
        self._warned_thresholds = set()
        self._stage = None

    @property
    def window(self):
        return tuple(self._window)

    @property
    def cluster_rounds(self):
        return tuple(round_item.model_round for round_item in self._cluster)

    def mark_stage_boundary(self, stage):
        self._stage = stage
        self._cluster = []
        self._warned_thresholds.clear()

    def _similarity(self, current, representative):
        if current.exact_hash == representative.exact_hash:
            return "exact", {"minhash_similarity": 1.0}
        similarity = _minhash_similarity(current.minhash, representative.minhash)
        if similarity >= self.minhash_similarity:
            return "near_duplicate", {"minhash_similarity": similarity}
        return None, {"minhash_similarity": similarity}

    def observe(self, model_output, model_round, stage=None, tool_names=(), tool_span_ids=()):
        canonical = canonicalize_model_output(model_output)
        current = _Round(
            model_round=int(model_round),
            stage=stage if stage is not None else self._stage,
            canonical=canonical,
            exact_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            minhash=_minhash(canonical, self.minhash_permutations),
            tool_names=tuple(str(name) for name in tool_names),
            tool_span_ids=tuple(str(span_id) for span_id in tool_span_ids if span_id),
        )
        self._window.append(current)
        if not self._cluster:
            self._cluster = [current]
            return None
        pattern_type, similarity = self._similarity(current, self._cluster[0])
        if pattern_type is None:
            self._cluster = [current]
            self._warned_thresholds.clear()
            return None
        self._cluster.append(current)
        if len(self._cluster) > self.window_size:
            self._cluster = self._cluster[-self.window_size:]
        repeated_rounds = len(self._cluster)
        for threshold in self.thresholds:
            if repeated_rounds >= threshold and threshold not in self._warned_thresholds:
                self._warned_thresholds.add(threshold)
                return RepetitionSignal(
                    threshold=threshold,
                    repeated_rounds=repeated_rounds,
                    pattern_type=pattern_type,
                    similarity=similarity,
                    matched_rounds=tuple(item.model_round for item in self._cluster),
                    model_round=current.model_round,
                    stage=current.stage,
                    tool_names=current.tool_names,
                    tool_span_ids=current.tool_span_ids,
                )
        return None
