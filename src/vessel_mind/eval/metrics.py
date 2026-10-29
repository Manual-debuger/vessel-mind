"""Retrieval metrics over ranked citation sources (spec §5.8).

A result's source is ``<source name>/<path>#<anchor>``; a golden expected
source is the document, ``<source name>/<path>``. A result matches an
expected source when its document is that source.
"""

from collections.abc import Sequence


def document(source: str) -> str:
    """The document of a citation source: the source without its heading anchor."""
    return source.split("#", 1)[0]


def _check(expected: Sequence[str]) -> None:
    if not expected:
        raise ValueError("a retrieval metric needs expected sources")


def recall_at_k(ranked: Sequence[str], expected: Sequence[str], *, k: int) -> float:
    """The share of expected documents that some result in the top k belongs to."""
    _check(expected)
    found = {document(source) for source in ranked[:k]}
    return sum(1 for e in set(expected) if e in found) / len(set(expected))


def reciprocal_rank(ranked: Sequence[str], expected: Sequence[str], *, k: int) -> float:
    """1 / the rank of the first top-k result from an expected document; 0 if none."""
    _check(expected)
    wanted = set(expected)
    for rank, source in enumerate(ranked[:k], start=1):
        if document(source) in wanted:
            return 1.0 / rank
    return 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """The q-th percentile, interpolated linearly between the closest ranks."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
