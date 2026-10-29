"""Citation correctness is computed by code (spec §5.8), with the console's rule:
[n] maps to result n of the latest successful search_docs call before that text."""

from pydantic import JsonValue

from vessel_mind.core.events import TextEvent, ToolResultEvent, ToolStartEvent
from vessel_mind.eval.citations import cited_sources, search_sources


def _search(call_id: str, *sources: str, is_error: bool = False):
    result: JsonValue = (
        {"error": "boom"}
        if is_error
        else {"results": [{"n": n, "source": s} for n, s in enumerate(sources, start=1)]}
    )
    return [
        ToolStartEvent(call_id=call_id, name="search_docs", input={"query": "q"}),
        ToolResultEvent(call_id=call_id, name="search_docs", result=result, is_error=is_error),
    ]


def test_a_citation_maps_to_the_latest_search_before_the_text():
    events = [
        *_search("a", "d/one.md#x", "d/two.md#y"),
        TextEvent(text="First [2]. "),
        *_search("b", "d/three.md#z"),
        TextEvent(text="Then [1]."),
    ]

    assert cited_sources(events) == ("d/two.md#y", "d/three.md#z")


def test_groups_split_across_deltas_and_repeats_count_once():
    events = [
        *_search("a", "d/one.md#x", "d/two.md#y"),
        TextEvent(text="A [1"),
        TextEvent(text="][2] and [1, 2]."),
    ]

    assert cited_sources(events) == ("d/one.md#x", "d/two.md#y")


def test_a_group_with_an_unknown_number_and_text_before_any_search_are_not_citations():
    events = [
        TextEvent(text="Before [1]. "),
        *_search("a", "d/one.md#x"),
        TextEvent(text="Bad [1, 3]. Good [1]."),
        *_search("b", is_error=True),
        TextEvent(text=" Still the first search [1]."),
    ]

    assert cited_sources(events) == ("d/one.md#x",)


def test_search_sources_lists_each_successful_search_in_order():
    events = [
        *_search("a", "d/one.md#x", "d/two.md#y"),
        *_search("b", is_error=True),
        *_search("c", "d/three.md#z"),
    ]

    assert search_sources(events) == (("d/one.md#x", "d/two.md#y"), ("d/three.md#z",))
