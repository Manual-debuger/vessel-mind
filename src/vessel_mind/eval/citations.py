"""The sources a turn's answer cites, read from its protocol events.

The rule is the console's (server/static/citations.js): [n] in the answer maps
to result n of the latest successful search_docs call before that text. Groups
such as [1][2] and [1, 2] work; a group with a number that search did not
return is plain text, as is a group before any search.
"""

import re
from collections.abc import Iterable, Sequence

from vessel_mind.core.events import AgentEvent, TextEvent, ToolResultEvent

SEARCH_TOOL = "search_docs"

_GROUP = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def _results(event: ToolResultEvent) -> tuple[str, ...] | None:
    """The sources of a successful search_docs result, numbered from 1; else None."""
    if event.name != SEARCH_TOOL or event.is_error or not isinstance(event.result, dict):
        return None
    results = event.result.get("results")
    if not isinstance(results, list):
        return None
    return tuple(str(r["source"]) for r in results if isinstance(r, dict) and "source" in r)


def search_sources(events: Iterable[AgentEvent]) -> tuple[tuple[str, ...], ...]:
    """Each successful search's sources, best first, in the order of the calls."""
    found = (_results(e) for e in events if isinstance(e, ToolResultEvent))
    return tuple(sources for sources in found if sources is not None)


def cited_sources(events: Iterable[AgentEvent]) -> tuple[str, ...]:
    """The distinct sources the answer cites, in the order first cited."""
    cited: dict[str, None] = {}
    latest: tuple[str, ...] = ()
    text: list[str] = []

    def flush() -> None:
        for source in _cite("".join(text), latest):
            cited.setdefault(source)
        text.clear()

    for event in events:
        if isinstance(event, TextEvent):
            text.append(event.text)
        elif isinstance(event, ToolResultEvent) and (sources := _results(event)) is not None:
            flush()
            latest = sources
    flush()
    return tuple(cited)


def _cite(text: str, results: Sequence[str]) -> list[str]:
    sources: list[str] = []
    for group in _GROUP.finditer(text):
        numbers = [int(n) for n in group.group(1).split(",")]
        if all(1 <= n <= len(results) for n in numbers):
            sources.extend(results[n - 1] for n in numbers)
    return sources
