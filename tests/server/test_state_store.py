"""The state file's store: migrations and the history it keeps."""

import sqlite3

import pytest

from vessel_mind.core.chat import AssistantMessage, ToolCall, ToolMessage, UserMessage
from vessel_mind.server.state import (
    MIGRATIONS,
    SessionStore,
    StateError,
    TurnLimitReached,
    UnknownSession,
)


def test_a_new_state_file_is_migrated_to_the_latest_schema(tmp_path):
    store = SessionStore.open(tmp_path / "nested" / "state.db")

    assert store.schema_version == len(MIGRATIONS)
    store.close()


def test_reopening_a_migrated_file_keeps_its_data(tmp_path):
    path = tmp_path / "state.db"
    store = SessionStore.open(path)
    session_id = store.create_session()
    turn_id = store.begin_turn(session_id)
    store.finish_turn(turn_id, "done", [UserMessage(text="hi"), AssistantMessage(text="hello")])
    store.close()

    store = SessionStore.open(path)
    assert store.history(session_id) == [UserMessage(text="hi"), AssistantMessage(text="hello")]
    assert store.turn_status(turn_id) == "done"
    store.close()


def test_tool_calls_with_provider_data_and_tool_results_round_trip_through_history(tmp_path):
    messages = [
        UserMessage(text="Weather in Paris?"),
        AssistantMessage(
            text="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="get_weather",
                    input={"city": "Paris"},
                    provider_data={"extra_content": {"google": {"thought_signature": "c2ln"}}},
                )
            ],
        ),
        ToolMessage(tool_call_id="call_1", content='{"temp_c": 18}'),
        AssistantMessage(text="18°C."),
    ]
    path = tmp_path / "state.db"
    store = SessionStore.open(path)
    session_id = store.create_session()
    store.finish_turn(store.begin_turn(session_id), "done", messages)
    store.close()

    store = SessionStore.open(path)
    assert store.history(session_id) == messages
    store.close()


def test_a_state_file_from_newer_code_is_refused(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()

    with pytest.raises(StateError, match="schema version"):
        SessionStore.open(path)


def test_history_appends_turn_after_turn_in_order(tmp_path):
    store = SessionStore.open(tmp_path / "state.db")
    session_id = store.create_session()
    for word in ["one", "two"]:
        turn_id = store.begin_turn(session_id)
        store.finish_turn(turn_id, "done", [UserMessage(text=word), AssistantMessage(text=word)])

    assert store.history(session_id) == [
        UserMessage(text="one"),
        AssistantMessage(text="one"),
        UserMessage(text="two"),
        AssistantMessage(text="two"),
    ]
    store.close()


def test_an_unknown_session_and_the_turn_limit_are_refused(tmp_path):
    store = SessionStore.open(tmp_path / "state.db")
    with pytest.raises(UnknownSession):
        store.begin_turn("0" * 32)
    with pytest.raises(UnknownSession):
        store.history("0" * 32)

    session_id = store.create_session()
    store.begin_turn(session_id, max_turns=1)
    with pytest.raises(TurnLimitReached):
        store.begin_turn(session_id, max_turns=1)
    store.close()
