"""The judge: for each key point, yes or no and the covering sentence (spec §5.8)."""

import json
from pathlib import Path

import pytest

from vessel_mind.core.chat import ChatModelError, ChatRequest, Stop, TextDelta, Usage
from vessel_mind.eval.judge import Judge, JudgeError, load_rubric

REPO = Path(__file__).resolve().parent.parent.parent


class Replies:
    """A fake judge model: replies with the given text and keeps the requests."""

    id = "fake-judge"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ChatRequest] = []

    async def stream(self, req: ChatRequest):
        self.requests.append(req)
        yield TextDelta(delta=self.text)
        yield Stop(reason="end", usage=Usage(input_tokens=5))


class Fails:
    id = "fake-judge"

    async def stream(self, req: ChatRequest):
        raise ChatModelError("HTTP 429")
        yield  # pragma: no cover


GOOD = """```json
{"verdicts": [{"key_point": 1, "covered": true, "quote": "It picks a path."},
              {"key_point": 2, "covered": false, "quote": null}]}
```"""


@pytest.mark.asyncio
async def test_the_judge_sends_the_rubric_and_the_numbered_key_points():
    model = Replies(GOOD)
    judge = Judge(model, "RUBRIC", max_tokens=99)

    verdicts = await judge.decide("Q?", ("picks a path", "falls back"), "It picks a path.")

    assert [(v.covered, v.quote) for v in verdicts] == [(True, "It picks a path."), (False, None)]
    request = model.requests[0]
    assert request.system == "RUBRIC"
    assert request.max_tokens == 99
    assert request.tools == []
    sent = json.loads(request.messages[0].text)  # type: ignore[union-attr]
    assert sent == {
        "question": "Q?",
        "key_points": [{"n": 1, "text": "picks a path"}, {"n": 2, "text": "falls back"}],
        "answer": "It picks a path.",
    }


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        # one verdict for two key points
        '{"verdicts": [{"key_point": 1, "covered": true, "quote": "x"}]}',
        # covered without a quote
        '{"verdicts": [{"key_point": 1, "covered": true, "quote": null},'
        ' {"key_point": 2, "covered": false, "quote": null}]}',
        # numbered out of order
        '{"verdicts": [{"key_point": 2, "covered": false, "quote": null},'
        ' {"key_point": 1, "covered": false, "quote": null}]}',
    ],
)
@pytest.mark.asyncio
async def test_a_reply_that_breaks_the_rubric_is_a_judge_error(reply):
    judge = Judge(Replies(reply), "RUBRIC")

    with pytest.raises(JudgeError):
        await judge.decide("Q?", ("a", "b"), "answer")


@pytest.mark.asyncio
async def test_a_model_error_is_a_judge_error():
    with pytest.raises(JudgeError, match="429"):
        await Judge(Fails(), "RUBRIC").decide("Q?", ("a",), "answer")


def test_the_committed_rubric_is_the_judge_prompt():
    rubric = load_rubric(REPO / "eval" / "judge.md")

    assert '"verdicts"' in rubric
    assert "quote" in rubric
