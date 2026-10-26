"""Retrieval metrics, checked against worked examples."""

import pytest

from vessel_mind.eval.metrics import percentile, recall_at_k, reciprocal_rank

RANKED = [
    "vm/GLOSSARY.md#session",
    "vm/docs/spec.md#router",
    "vm/GLOSSARY.md#proposal",
    "vc/README.md",
    "vc/docs/usage.md#exit-codes",
    "vc/docs/adopting.md",
]


def test_recall_counts_expected_documents_found_in_the_top_k_once_each():
    # The glossary appears twice in the top 5 but is one document; adopting.md is 6th.
    expected = ["vm/GLOSSARY.md", "vc/docs/usage.md", "vc/docs/adopting.md", "vm/ADR.md"]

    assert recall_at_k(RANKED, expected, k=5) == 0.5


def test_reciprocal_rank_is_one_over_the_rank_of_the_first_expected_result():
    assert reciprocal_rank(RANKED, ["vc/README.md"], k=5) == 0.25
    assert reciprocal_rank(RANKED, ["vm/GLOSSARY.md"], k=5) == 1.0


def test_reciprocal_rank_is_zero_when_no_expected_document_is_in_the_top_k():
    assert reciprocal_rank(RANKED, ["vc/docs/adopting.md"], k=5) == 0.0


def test_metrics_need_expected_sources():
    with pytest.raises(ValueError, match="expected"):
        recall_at_k(RANKED, [], k=5)


def test_percentiles_interpolate_between_the_closest_ranks():
    values = [40.0, 10.0, 30.0, 20.0]

    assert percentile(values, 50) == 25.0
    assert percentile(values, 95) == pytest.approx(38.5)
    assert percentile([7.0], 95) == 7.0
