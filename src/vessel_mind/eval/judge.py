"""The judge (spec §5.8): for each key point, covered or not, and the sentence that covers it.

The judge is any ChatModel set in config (eval.judge); its system prompt is the
committed rubric, eval/judge.md. So a change of judge or rubric is a config or
file change, not a code change.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vessel_mind.core.chat import (
    ChatModel,
    ChatModelError,
    ChatRequest,
    Usage,
    UserMessage,
    read_reply,
    unfenced,
)

DEFAULT_RUBRIC = Path("eval/judge.md")


class JudgeError(Exception):
    """The judge failed or its reply broke the rubric. The answer stays unscored."""


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_point: int = Field(ge=1)
    covered: bool
    quote: str | None


class _Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: tuple[Verdict, ...]


def load_rubric(path: Path = DEFAULT_RUBRIC) -> str:
    return path.read_text(encoding="utf-8")


class Judge:
    def __init__(self, model: ChatModel, rubric: str, *, max_tokens: int | None = None) -> None:
        self.model = model
        self.rubric = rubric
        self.max_tokens = max_tokens
        self.usage = Usage()  # summed over every decision, for the record

    async def decide(
        self, question: str, key_points: Sequence[str], answer: str
    ) -> tuple[Verdict, ...]:
        """One verdict per key point, in order. Raises JudgeError."""
        prompt = {
            "question": question,
            "key_points": [{"n": n, "text": text} for n, text in enumerate(key_points, start=1)],
            "answer": answer,
        }
        request = ChatRequest(
            system=self.rubric,
            messages=[UserMessage(text=json.dumps(prompt, ensure_ascii=False))],
            max_tokens=self.max_tokens,
        )
        try:
            reply = await read_reply(self.model, request)
        except ChatModelError as error:
            raise JudgeError(f"judge {self.model.id}: {error}") from None
        self.usage += reply.usage
        try:
            verdicts = _Verdicts.model_validate_json(unfenced(reply.text)).verdicts
        except ValidationError:
            raise JudgeError(f"judge {self.model.id}: the reply is not the rubric's JSON") from None
        if [v.key_point for v in verdicts] != list(range(1, len(key_points) + 1)):
            raise JudgeError(
                f"judge {self.model.id}: {len(verdicts)} verdicts for {len(key_points)} key points"
            )
        if any(v.covered and not (v.quote or "").strip() for v in verdicts):
            raise JudgeError(f"judge {self.model.id}: a covered key point has no quote")
        return verdicts
