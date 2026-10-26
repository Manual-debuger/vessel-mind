"""The committed JSON Schema of the event protocol matches core/events.py."""

from pathlib import Path

from vessel_mind.core.events import event_schema, event_schema_text

COMMITTED = Path(__file__).resolve().parent.parent.parent / "schema" / "events.schema.json"


def test_committed_event_schema_matches_the_models():
    assert COMMITTED.read_text(encoding="utf-8") == event_schema_text(), (
        "schema/events.schema.json is stale: run `task schema:export` and commit it."
    )


def test_event_schema_names_every_protocol_event():
    mapping = event_schema()["discriminator"]["mapping"]

    assert sorted(mapping) == ["done", "error", "text"]


def test_every_field_a_client_receives_is_required_in_the_schema():
    # Generated client types must not mark the discriminator, or any other
    # field the server always sends, as optional.
    defs = event_schema()["$defs"]

    for name in ["TextEvent", "DoneEvent", "ErrorEvent", "Usage"]:
        assert sorted(defs[name]["required"]) == sorted(defs[name]["properties"]), name
