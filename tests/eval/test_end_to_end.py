"""task eval -- --config hosted|local|routed: table 2, end to end (spec §5.8).

A fixture golden set, fake fast and slow models, a fake judge and a fake search
behind the real search_docs tool. No test calls a live API. Every expected
number below is worked out by hand from the scripts in this file.
"""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from vessel_mind.agent.router import ROUTER_SYSTEM, Router
from vessel_mind.core.capabilities import CapabilityRegistry
from vessel_mind.core.chat import (
    ChatModelError,
    ChatRequest,
    Stop,
    TextDelta,
    ToolCall,
    Usage,
    UserMessage,
)
from vessel_mind.eval.end_to_end import (
    NotRun,
    ResumeError,
    RowSpec,
    run_end_to_end,
    store_not_run,
)
from vessel_mind.eval.golden import load_golden
from vessel_mind.eval.judge import Judge
from vessel_mind.eval.pacing import PacedModel, Pacer
from vessel_mind.eval.records import Row
from vessel_mind.eval.results import model_family, render_results
from vessel_mind.rag.search import SearchResult
from vessel_mind.rag.search_docs import SearchDocsTool
from vessel_mind.server.config import ConfigurationName, ModelPrice
from vessel_mind.server.retrieval import SEARCH_DOCS_LABELS

TODAY = date(2026, 10, 30)
PRICED = date(2026, 10, 26)
SOURCE = "https://example.com/pricing"

GOLDEN = """
questions:
  - id: en-01
    question: "How does the router choose?"
    lang: en
    expected_sources: [fixture/router.md]
    key_points: ["picks a path", "falls back to slow"]
    expected_route: fast
    rationale: "One retrieval."
  - id: en-02
    question: "Compare the router and the session."
    lang: en
    expected_sources: [fixture/router.md, fixture/session.md]
    key_points: ["picks a path", "holds turns"]
    expected_route: slow
    rationale: "Two topics."
  - id: un-01
    question: "What is a unicorn?"
    lang: en
    expected_sources: []
    key_points: ["do not cover it"]
    expected_route: fast
    rationale: "Unanswerable."
zh_hant_questions:
  - id: zh-01
    question: "會話 session 是什麼？"
    lang: zh-Hant
    expected_sources: [fixture/session.md]
    key_points: ["holds turns"]
    expected_route: fast
    rationale: "一次檢索。"
"""

# The fast model's classification of each question: en-02 is a misroute
# (expected slow), un-01 is not JSON, so it falls back to the slow path.
ROUTES = {
    "How does the router choose?": '{"route": "fast"}',
    "Compare the router and the session.": '{"route": "fast"}',
    "What is a unicorn?": "fast, I think",
    "會話 session 是什麼？": '{"route": "fast"}',
}
FAST_ANSWERS = {
    "How does the router choose?": "The router picks a path [1].",
    "Compare the router and the session.": "The router picks a path [1].",
    "會話 session 是什麼？": "A session holds turns [1] and zebras [2].",
}
SLOW_ANSWERS = {
    "How does the router choose?": "The router picks a path and falls back to slow [1].",
    "Compare the router and the session.": "The router picks a path [1]; a session holds turns [2].",
    "What is a unicorn?": "The documents do not cover it.",
    "會話 session 是什麼？": "A session holds turns [1].",
}
CLASSIFY = Usage(input_tokens=10, output_tokens=1)
FAST_ANSWER = Usage(input_tokens=100, output_tokens=10)
SLOW_STEP = Usage(input_tokens=200, output_tokens=20, thinking_tokens=5)


class Scripted:
    """A fake ChatModel: reply(request) gives one step's events."""

    def __init__(self, id: str, reply) -> None:
        self.id = id
        self.reply = reply
        self.calls = 0

    async def stream(self, req: ChatRequest):
        self.calls += 1
        for event in self.reply(req):
            yield event


def _user(req: ChatRequest) -> str:
    first = req.messages[0]
    assert isinstance(first, UserMessage)
    return first.text


def fast_reply(req: ChatRequest):
    if req.system == ROUTER_SYSTEM:
        return [TextDelta(delta=ROUTES[_user(req)]), Stop(reason="end", usage=CLASSIFY)]
    return [TextDelta(delta=FAST_ANSWERS[_user(req)]), Stop(reason="end", usage=FAST_ANSWER)]


def slow_reply(req: ChatRequest):
    question = _user(req)
    if len(req.messages) == 1:
        call = ToolCall(id="c1", name="search_docs", input={"query": question})
        return [call, Stop(reason="tool_use", usage=SLOW_STEP)]
    return [TextDelta(delta=SLOW_ANSWERS[question]), Stop(reason="end", usage=SLOW_STEP)]


def failing_reply(req: ChatRequest):
    raise ChatModelError("HTTP 429: quota")


def judge_reply(req: ChatRequest):
    """Covered when the key point's text is in the answer; the quote is the answer."""
    sent = json.loads(_user(req))
    answer = sent["answer"]
    verdicts = [
        {
            "key_point": kp["n"],
            "covered": kp["text"] in answer.lower(),
            "quote": answer if kp["text"] in answer.lower() else None,
        }
        for kp in sent["key_points"]
    ]
    return [TextDelta(delta=json.dumps({"verdicts": verdicts})), Stop(reason="end", usage=Usage())]


def search(query: str) -> list[SearchResult]:
    names = [n for n in ("router", "session") if n in query.lower()] + ["zebra"]
    return [
        SearchResult(
            source=f"fixture/{n}.md#{n}",
            url=f"https://github.com/example/fixture/blob/{'a' * 40}/{n}.md#{n}",
            heading_path=(n.title(),),
            text=f"About {n}.",
            score=1.0,
        )
        for n in names
    ]


class Clock:
    """Each reading is 1 s after the last: every turn takes 1 s."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


PRICES = {
    "fake-lite": ModelPrice(
        input_usd_per_mtok=1.0, output_usd_per_mtok=2.0, as_of=PRICED, source=SOURCE
    ),
    "fake-flash": ModelPrice(
        input_usd_per_mtok=10.0, output_usd_per_mtok=20.0, as_of=PRICED, source=SOURCE
    ),
}


class Models:
    def __init__(self, fast=fast_reply, slow=slow_reply, judge=judge_reply) -> None:
        self.fast = Scripted("fake-lite", fast)
        self.slow = Scripted("fake-flash", slow)
        # The judge is the slow model's id: a self-family case.
        self.judge = Scripted("fake-flash", judge)


def _golden(tmp_path: Path):
    path = tmp_path / "golden.yaml"
    path.write_text(GOLDEN, encoding="utf-8")
    return load_golden(path)


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(SearchDocsTool(search), SEARCH_DOCS_LABELS)
    return registry


async def _run(
    tmp_path: Path,
    *,
    router_on: bool,
    models: Models | None = None,
    golden=None,
    configuration: ConfigurationName = "hosted",
    **kwargs,
):
    models = models or Models()
    pacer = Pacer(0.0)
    return await run_end_to_end(
        golden=golden or _golden(tmp_path),
        spec=RowSpec(
            row=Row(configuration, router_on),
            fast_model="fake-lite",
            slow_model="fake-flash",
            prices=PRICES,
            self_hosted_models=(),
        ),
        router=Router(
            PacedModel(models.fast, pacer),
            PacedModel(models.slow, pacer),
            enabled=router_on,
            timeout=5.0,
        ),
        tools=_registry(),
        judge=Judge(PacedModel(models.judge, pacer), "RUBRIC"),
        pacer=pacer,
        results_dir=tmp_path / "results",
        top_k=5,
        clock=Clock(),
        today=TODAY,
        **kwargs,
    )


def _not_run(tmp_path: Path) -> None:
    rows: tuple[tuple[ConfigurationName, str, str], ...] = (
        ("local", "local-small", "local-large"),
        ("routed", "local-small", "fake-flash"),
    )
    for name, fast, slow in rows:
        store_not_run(
            tmp_path / "results",
            NotRun(
                row=name,
                configuration=name,
                fast_model=fast,
                slow_model=slow,
                reason="The local model server is not set up; the author chose this.",
            ),
        )


async def _all_rows(tmp_path: Path) -> str:
    _not_run(tmp_path)
    await _run(tmp_path, router_on=False)
    await _run(tmp_path, router_on=True)
    return (tmp_path / "results" / "results.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_table_2_has_every_row_with_measured_values_or_not_run(tmp_path):
    text = await _all_rows(tmp_path)

    assert "## 2. End to end" in text
    assert (
        "| Row | Fast / slow model | Questions | recall@5 | MRR@5 | Answer score "
        "| Citation correctness | Turn p50 s | Turn p95 s | Tokens in / out "
        "| List-price cost (USD) |"
    ) in text
    # hosted: answer scores 0.5, 0.5, 1, 1; citations router, router, session (right)
    # and zebra (wrong); tokens and cost from the usages and prices above.
    assert (
        "| hosted | fake-lite / fake-flash | 4 of 4 | 1.00 | 1.00 | 0.75 | 0.75 (3 of 4) "
        "| 1.0 | 1.0 | 740 / 84 | $0.0054 at list price, 2026-10-26 |"
    ) in text
    assert (
        "| slow-only | router off / fake-flash | 4 of 4 | 1.00 | 1.00 | 1.00 | 1.00 (4 of 4) "
        "| 1.0 | 1.0 | 1,600 / 200 | $0.0200 at list price, 2026-10-26 |"
    ) in text
    for row, models in (
        ("local", "local-small / local-large"),
        ("routed", "local-small / fake-flash"),
    ):
        assert f"| {row} | {models} | *not run* |" in text
        assert (
            f"- **{row}** was not run: The local model server is not set up; the author chose this."
        ) in text
    assert text.index("| hosted |") < text.index("| slow-only |") < text.index("| local |")
    assert text.index("| local |") < text.index("| routed |")


@pytest.mark.asyncio
async def test_routed_rows_report_router_accuracy_the_confusion_table_and_costly_misroutes(
    tmp_path,
):
    text = await _all_rows(tmp_path)

    assert "### Router: hosted" in text
    assert "Router accuracy against `expected_route`: 0.50 (2 of 4 turns)." in text
    assert "Fallbacks to the slow path: 1 (invalid: 1)." in text
    assert (
        "| Expected \\ routed | fast | slow |\n|---|---|---|\n| fast | 2 | 1 |\n| slow | 1 | 0 |"
        in text
    )
    # en-01 and en-02 scored 0.5 on the fast path and 1.0 slow-only; zh-01 tied.
    assert "Costly misroutes: 2 (en-01, en-02)." in text
    assert "### Router: slow-only" not in text


@pytest.mark.parametrize(
    ("judge", "measured"),
    [
        ("qwen3.6:35b-a3b", "qwen3:8b"),
        ("gemini-3.6-flash", "gemini-3.5-flash-lite"),
        ("models/gemini-3.6-flash", "gemini-3.5-flash"),
    ],
)
def test_models_whose_names_begin_alike_are_one_family(judge, measured):
    assert model_family(judge) == model_family(measured)


def test_models_whose_names_begin_differently_are_different_families():
    assert model_family("qwen3:8b") != model_family("gemini-3.5-flash")


@pytest.mark.asyncio
async def test_the_judge_is_named_and_a_shared_model_family_is_stated(tmp_path):
    text = await _all_rows(tmp_path)

    assert "Judge: `fake-flash`" in text
    assert (
        "The judge `fake-flash` shares a model family (`fake`) with the measured rows "
        "hosted, slow-only"
    ) in text


@pytest.mark.asyncio
async def test_misroutes_compare_with_the_slow_only_row_of_the_same_configuration(tmp_path):
    await _run(tmp_path, router_on=True, configuration="local")
    await _run(tmp_path, router_on=False, configuration="local")

    text = render_results(tmp_path / "results")

    assert "### Router: local\n" in text
    assert "Costly misroutes: 2 (en-01, en-02)." in text
    assert "### Router: local-slow-only" not in text
    assert "| slow-only | — | *not run* |" in text


@pytest.mark.asyncio
async def test_without_a_slow_only_row_misroutes_are_not_computed(tmp_path):
    await _run(tmp_path, router_on=True)

    text = render_results(tmp_path / "results")

    assert "Costly misroutes: not computed: the slow-only row has not run." in text
    # The missing rows are listed, never omitted.
    assert "| slow-only | — | *not run* |" in text
    assert "| local | — | *not run* |" in text


@pytest.mark.asyncio
async def test_self_hosted_models_cost_nothing_and_say_electricity_is_not_measured(tmp_path):
    from vessel_mind.eval.results import cost_cell

    assert (
        cost_cell(0.0, dates=(), self_hosted=True, priced=False, missing=())
        == "$0 (GPU electricity not measured)"
    )
    assert (
        cost_cell(0.0123, dates=(PRICED,), self_hosted=True, priced=True, missing=())
        == "$0.0123 at list price, 2026-10-26; self-hosted models $0, GPU electricity not measured"
    )
    assert (
        cost_cell(None, dates=(), self_hosted=False, priced=False, missing=("x-1",))
        == "not reported: no list price for `x-1` in config"
    )


@pytest.mark.asyncio
async def test_a_failed_question_is_reported_and_retried_on_the_next_run(tmp_path):
    def fails_on_unicorn(req: ChatRequest):
        if _user(req) == "What is a unicorn?":
            raise ChatModelError("HTTP 429: quota")
        return slow_reply(req)

    first = Models(slow=fails_on_unicorn)
    await _run(tmp_path, router_on=True, models=first)
    text = render_results(tmp_path / "results")
    assert "| hosted | fake-lite / fake-flash | 3 of 4 (1 failed) |" in text

    again = Models()
    await _run(tmp_path, router_on=True, models=again)

    # Only un-01 ran again: its classification, two slow steps and its judgement.
    assert (again.fast.calls, again.slow.calls, again.judge.calls) == (1, 2, 1)
    text = render_results(tmp_path / "results")
    assert "| hosted | fake-lite / fake-flash | 4 of 4 | 1.00 | 1.00 | 0.75 |" in text


@pytest.mark.asyncio
async def test_an_answer_the_judge_failed_on_is_judged_again_without_a_new_turn(tmp_path):
    await _run(
        tmp_path, router_on=False, models=Models(judge=failing_reply), max_consecutive_failures=9
    )
    text = render_results(tmp_path / "results")
    assert (
        "| slow-only | router off / fake-flash | 4 of 4 | 1.00 | 1.00 | n/a (4 not judged) |"
        in text
    )

    again = Models()
    await _run(tmp_path, router_on=False, models=again)

    assert (again.fast.calls, again.slow.calls, again.judge.calls) == (0, 0, 4)
    assert "| 1.00 | 1.00 | 1.00 | 1.00 (4 of 4) |" in render_results(tmp_path / "results")


@pytest.mark.asyncio
async def test_the_run_stops_after_consecutive_failures_and_reports_the_row_incomplete(tmp_path):
    models = Models(slow=failing_reply)

    results = await _run(tmp_path, router_on=False, models=models, max_consecutive_failures=2)

    assert models.slow.calls == 2
    assert results.stopped is not None
    text = render_results(tmp_path / "results")
    assert "| slow-only | router off / fake-flash | 0 of 4 (2 failed, 2 not yet run) |" in text
    assert "*incomplete*" in text


REPO = Path(__file__).resolve().parent.parent.parent
EXAMPLE = REPO / "config" / "agent.example.yaml"


def test_a_configuration_marked_not_run_is_refused_and_listed_with_its_reason(tmp_path, capsys):
    from vessel_mind.eval.run import main

    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["eval"]["not_run"] = {
        "hosted": "The free tier is spent.",
        "local": "The local model server is off.",
    }
    config = tmp_path / "agent.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = tmp_path / "results"
    code = main(["--config", "local", "--config-file", str(config), "--results-dir", str(results)])

    assert code == 2
    assert "not run" in capsys.readouterr().err
    text = (results / "results.md").read_text(encoding="utf-8")
    assert "| local | qwen3:8b / qwen3.6:35b-a3b | *not run* |" in text
    assert "- **local** was not run: The local model server is off." in text
    assert "| hosted | gemini-3.5-flash-lite / gemini-3.6-flash | *not run* |" in text
    # A configuration that is not run has no slow-only row either.
    assert "| slow-only | router off / gemini-3.6-flash | *not run* |" in text
    assert "- **slow-only** was not run: The free tier is spent." in text
    assert "| routed | — | *not run* |" in text


def test_a_run_without_the_key_is_refused_before_any_request(tmp_path, monkeypatch, capsys):
    from vessel_mind.eval.run import main

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["eval"]["not_run"] = {}
    config = tmp_path / "agent.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    results = tmp_path / "results"

    code = main(
        ["--config", "hosted", "--no-router", "--config-file", str(config)]
        + ["--results-dir", str(results)]
    )

    assert code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert not (results / "end_to_end-slow-only.json").exists()


@pytest.mark.asyncio
async def test_render_regenerates_results_md_from_the_stored_rows(tmp_path):
    from vessel_mind.eval.run import main

    expected = await _all_rows(tmp_path)
    results = tmp_path / "results"
    (results / "results.md").write_text("edited by hand\n", encoding="utf-8")

    assert main(["--render", "--results-dir", str(results)]) == 0
    assert (results / "results.md").read_text(encoding="utf-8") == expected


def test_the_example_config_names_the_judge_and_dated_list_prices():
    from vessel_mind.server.config import load_config

    settings = load_config(EXAMPLE).eval
    assert settings is not None
    # The free tier allows 20 gemini-3.6-flash requests a day, so the judge is local.
    assert (settings.judge.provider, settings.judge.model) == ("local", "qwen3.6:35b-a3b")
    for model in ("gemini-3.5-flash-lite", "gemini-3.6-flash"):
        price = settings.prices[model]
        assert price.source.startswith("https://ai.google.dev/")
        assert price.as_of == date(2026, 10, 26)
    assert set(settings.not_run) == {"hosted", "routed"}
    assert settings.self_hosted_providers == ["local"]


class Author:
    """Answers every prompt with the next reply, and keeps what it was shown."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.shown: list[str] = []

    def ask(self, prompt: str) -> str:
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def show(self, text: str) -> None:
        self.shown.append(text)


@pytest.mark.asyncio
async def test_the_sample_to_grade_is_fixed_by_the_seed(tmp_path):
    from vessel_mind.eval.grades import sample_answers
    from vessel_mind.eval.records import load_rows

    await _all_rows(tmp_path)
    rows = load_rows(tmp_path / "results")

    all_answers = sample_answers(rows)
    three = [(row, r.id) for row, r in sample_answers(rows, n=3)]

    # 8 judged answers, fewer than 10: all of them, each once.
    assert sorted((row, r.id) for row, r in all_answers) == sorted(
        (row, q) for row in ("hosted", "slow-only") for q in ("en-01", "en-02", "un-01", "zh-01")
    )
    assert three == [(row, r.id) for row, r in sample_answers(rows, n=3)]
    assert len(set(three)) == 3


@pytest.mark.asyncio
async def test_the_author_grades_each_sampled_answer_once_and_agreement_is_reported(tmp_path):
    from vessel_mind.eval.grades import grade

    await _all_rows(tmp_path)
    results = tmp_path / "results"
    author = Author("y")

    assert grade(results, ask=author.ask, show=author.show, today=TODAY) == 8
    assert grade(results, ask=author.ask, show=author.show, today=TODAY) == 0

    lines = (tmp_path / "human_grades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    # The author sees the question, the answer and the key points, never the verdicts.
    assert "Question: How does the router choose?" in author.shown
    assert not any("covered" in line.lower() for line in author.shown)
    text = render_results(results)
    # The judge said no to the second key point of en-01 and en-02 on the fast
    # path (2 of 12); the author said yes to all 12.
    assert "### Judge–human agreement" in text
    assert "Judge and author agree on 10 of 12 key points (0.83)." in text
    assert "Mean answer score on these answers: judge 0.88, author 1.00." in text


@pytest.mark.asyncio
async def test_q_stops_grading_and_a_grade_of_a_replaced_answer_is_not_counted(tmp_path):
    from vessel_mind.eval.grades import grade

    await _all_rows(tmp_path)
    results = tmp_path / "results"

    assert grade(results, ask=Author("maybe", "q").ask, show=lambda _: None, today=TODAY) == 0
    assert not (tmp_path / "human_grades.jsonl").exists()
    assert "*Not graded yet*" in render_results(results)

    grade(results, ask=Author("y").ask, show=lambda _: None, today=TODAY)
    await _run(tmp_path, router_on=False, fresh=True, models=Models(slow=_reworded))

    text = render_results(results)
    assert "4 stored grades are for answers a later run replaced; they are not counted." in text


def _reworded(req: ChatRequest):
    events = slow_reply(req)
    return [
        TextDelta(delta=e.delta + " Indeed.") if isinstance(e, TextDelta) else e for e in events
    ]


@pytest.mark.asyncio
async def test_resuming_with_another_golden_set_is_refused_unless_fresh(tmp_path):
    await _run(tmp_path, router_on=False)
    changed = tmp_path / "changed.yaml"
    changed.write_text(GOLDEN + "\n# changed\n", encoding="utf-8")

    with pytest.raises(ResumeError, match="golden"):
        await _run(tmp_path, router_on=False, golden=load_golden(changed))

    again = Models()
    await _run(tmp_path, router_on=False, models=again, golden=load_golden(changed), fresh=True)
    assert again.slow.calls == 8
