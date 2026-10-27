"""``task eval``: the eval entry point (spec §5.8).

    task eval -- --retrieval-only               table 1, no LLM calls
    task eval -- --config hosted                table 2, one row: the router on
    task eval -- --config hosted --no-router    table 2, the slow-only row
    task eval -- --grade                        the author grades 10 sampled answers
    task eval -- --render                       regenerate results.md from the stored results

The eval refuses to run (exit 2) when a corpus source cannot be pinned or an
expected source of the golden set is not in the corpus at its pin. Before it
measures, it brings the index up to the pinned corpus (an unchanged index is a
no-op), so the numbers are for the pinned documents. Settings come from the
server's config (`retrieval:`, `agent:`, `router:`, `eval:`), so the eval
measures what the server runs; the flags override paths.

An end-to-end run stores each question as it finishes, so a run stopped by a
quota or by Ctrl-C resumes when the same command runs again. It exits 1 when
the row is incomplete.
"""

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from vessel_mind.eval.end_to_end import ResumeError, RowSpec, run_end_to_end, store_not_run
from vessel_mind.eval.golden import GoldenSet, GoldenSetError, load_golden
from vessel_mind.eval.grades import grade
from vessel_mind.eval.judge import DEFAULT_RUBRIC, Judge, load_rubric
from vessel_mind.eval.pacing import PacedModel, Pacer
from vessel_mind.eval.records import (
    ROW_ORDER,
    NotRun,
    RowResults,
    read_row,
    row_name,
    row_path,
)
from vessel_mind.eval.results import store_retrieval, write_results
from vessel_mind.eval.retrieval import Pin, RetrievalAblation, run_rows
from vessel_mind.rag.corpus import CorpusPinError, Document
from vessel_mind.rag.embed import Embedder, FastEmbedEmbedder
from vessel_mind.rag.ingest import corpus_loader, ingest
from vessel_mind.rag.rerank import CrossEncoderReranker, FastEmbedCrossEncoder, make_reranker
from vessel_mind.rag.search import Reranker
from vessel_mind.rag.store import IndexStore
from vessel_mind.server.config import AgentConfig, ConfigError, EvalSettings, load_config
from vessel_mind.server.main import DEFAULT_CONFIG, EXAMPLE_CONFIG, build_model, build_router
from vessel_mind.server.retrieval import RetrievalService, search_docs_registry

DEFAULT_GOLDEN = Path("eval/golden.yaml")
DEFAULT_RESULTS = Path("eval/results")
CONFIGURATIONS = ("hosted", "local", "routed")


def run_retrieval_only(
    *,
    golden: GoldenSet,
    load_documents: Callable[[], Iterable[Document]],
    index_path: Path,
    make_models: Callable[[], tuple[Embedder, Reranker]],
    results_dir: Path,
    candidate_k: int,
    top_k: int,
    clock: Callable[[], float] = time.perf_counter,
    today: date | None = None,
) -> RetrievalAblation:
    """Table 1. Raises CorpusPinError or GoldenSetError before it loads a model,
    touches the index or writes a result."""
    documents = list(load_documents())
    golden.check_sources({f"{d.source_name}/{d.path}" for d in documents})
    embedder, reranker = make_models()
    with IndexStore.open(index_path, embedder) as store:
        ingest(documents, store, embedder)
        rows, questions = run_rows(
            golden.questions,
            store,
            embedder,
            reranker,
            candidate_k=candidate_k,
            top_k=top_k,
            clock=clock,
        )
        chunks = store.count_chunks()
    pins = dict.fromkeys(Pin(name=d.source_name, repo=d.repo, commit=d.commit) for d in documents)
    ablation = RetrievalAblation(
        generated=today or datetime.now(UTC).date(),
        golden_path=golden.path.as_posix(),
        golden_sha256=golden.sha256,
        corpus=tuple(pins),
        chunks=chunks,
        embedding_model=embedder.model_id,
        reranker_model=reranker.model_id,
        candidate_k=candidate_k,
        top_k=top_k,
        rows=rows,
        questions=questions,
    )
    store_retrieval(results_dir, ablation)
    return ablation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task eval", description="Run the eval.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--retrieval-only",
        action="store_true",
        help="table 1, the retrieval ablation; no LLM calls",
    )
    mode.add_argument(
        "--config",
        choices=CONFIGURATIONS,
        help="table 2: run this configuration's row end to end",
    )
    mode.add_argument(
        "--grade", action="store_true", help="grade the sampled answers by hand (the author)"
    )
    mode.add_argument(
        "--render", action="store_true", help="regenerate results.md from the stored results"
    )
    parser.add_argument(
        "--no-router",
        action="store_true",
        help="with --config: every turn takes the slow path (hosted: the slow-only row)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="with --config: discard the row's stored results and start it again",
    )
    parser.add_argument(
        "--config-file", type=Path, help="default: config/agent.yaml, else the example"
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--corpus", type=Path, help="default: retrieval.corpus")
    parser.add_argument("--repo-root", type=Path, default=Path())
    parser.add_argument("--index", type=Path, help="default: retrieval.index")
    parser.add_argument("--tarball-cache", type=Path, help="default: retrieval.tarball_cache")
    parser.add_argument("--model-cache", type=Path, help="default: retrieval.model_cache")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if (args.no_router or args.fresh) and args.config is None:
        parser.error("--no-router and --fresh need --config")

    if args.render:
        print(write_results(args.results_dir))
        return 0
    if args.grade:
        graded = grade(args.results_dir)
        print(f"{graded} answers graded; {write_results(args.results_dir)} regenerated")
        return 0

    config_path = args.config_file or (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG
    )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    if args.config:
        return _end_to_end(args, config)
    try:
        golden = load_golden(args.golden)
    except GoldenSetError as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    return _retrieval_only(args, config, golden)


def _retrieval_only(args: argparse.Namespace, config: AgentConfig, golden: GoldenSet) -> int:
    settings = config.retrieval
    model_cache = args.model_cache or settings.model_cache

    def make_models() -> tuple[Embedder, Reranker]:
        embedder = FastEmbedEmbedder(settings.embedding_model, cache_dir=model_cache)
        encoder = FastEmbedCrossEncoder(settings.reranker_model, cache_dir=model_cache)
        return embedder, CrossEncoderReranker(encoder, max_candidates=settings.candidate_k)

    try:
        ablation = run_retrieval_only(
            golden=golden,
            load_documents=_documents(args, config),
            index_path=args.index or settings.index,
            make_models=make_models,
            results_dir=args.results_dir,
            candidate_k=settings.candidate_k,
            top_k=settings.top_k,
        )
    except (CorpusPinError, GoldenSetError) as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"{args.results_dir}: table 1 for {len(golden.questions)} questions, "
        f"{ablation.chunks} chunks, reranker {ablation.reranker_model}"
    )
    return 0


def _documents(args: argparse.Namespace, config: AgentConfig) -> Callable[[], Iterable[Document]]:
    settings = config.retrieval
    return corpus_loader(
        args.corpus or settings.corpus,
        repo_root=args.repo_root,
        cache_dir=args.tarball_cache or settings.tarball_cache,
    )


def _record_not_run(config: AgentConfig, settings: EvalSettings, results_dir: Path) -> None:
    """Stores each configuration marked not_run as not run, with its reason, unless
    the row has measured results. Its slow-only row is stored too when table 2
    lists it (`slow-only`, for hosted)."""
    for name, reason in settings.not_run.items():
        selected = config.select(name)
        for router in (True, False):
            row = row_name(name, router)
            if not router and row not in ROW_ORDER:
                continue
            path = row_path(results_dir, row)
            if path.exists() and isinstance(read_row(path), RowResults):
                continue
            store_not_run(
                results_dir,
                NotRun(
                    row=row,
                    configuration=name,
                    fast_model=selected.fast.model,
                    slow_model=selected.slow.model,
                    reason=reason,
                    router=router,
                ),
            )


def _end_to_end(
    args: argparse.Namespace, config: AgentConfig, environ: Mapping[str, str] | None = None
) -> int:
    environ = os.environ if environ is None else environ
    settings = config.eval
    if settings is None:
        print("eval refused: the config has no eval: section (judge, prices)", file=sys.stderr)
        return 2
    _record_not_run(config, settings, args.results_dir)
    if args.config in settings.not_run:
        print(
            f"eval refused: configuration {args.config!r} is marked not run in eval.not_run: "
            f"{settings.not_run[args.config]}",
            file=sys.stderr,
        )
        return 2

    name = row_name(args.config, not args.no_router)
    selected = config.select(args.config)
    pacer = Pacer(settings.request_interval)
    try:
        # A row measures its configuration's fixed pair, so no fallback answers.
        router = build_router(
            config,
            environ,
            configuration=args.config,
            enabled=not args.no_router,
            fallbacks=False,
            wrap=lambda model: PacedModel(model, pacer),
        )
        judge = Judge(
            PacedModel(build_model(config, settings.judge, environ), pacer),
            load_rubric(args.rubric),
            max_tokens=settings.judge_max_tokens,
        )
        golden = load_golden(args.golden)
        documents = list(_documents(args, config)())
        golden.check_sources({f"{d.source_name}/{d.path}" for d in documents})
    except (ConfigError, GoldenSetError, CorpusPinError, OSError) as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    self_hosted = set(settings.self_hosted_providers)
    used = (selected.fast, selected.slow) if not args.no_router else (selected.slow,)
    spec = RowSpec(
        name=name,
        configuration=args.config,
        router=not args.no_router,
        fast_model=selected.fast.model,
        slow_model=selected.slow.model,
        prices={c.model: settings.prices[c.model] for c in used if c.model in settings.prices},
        self_hosted_models=tuple(c.model for c in used if c.provider in self_hosted),
    )

    retrieval_settings = config.retrieval
    model_cache = args.model_cache or retrieval_settings.model_cache
    index_path = args.index or retrieval_settings.index
    try:
        embedder = FastEmbedEmbedder(retrieval_settings.embedding_model, cache_dir=model_cache)
        reranker = make_reranker(
            retrieval_settings.reranker, retrieval_settings.reranker_model, cache_dir=model_cache
        )
    except ValueError as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    with IndexStore.open(index_path, embedder) as store:
        ingest(documents, store, embedder)

    async def run() -> RowResults:
        async with RetrievalService(
            index_path=index_path,
            load_documents=lambda: documents,
            embedder=embedder,
            candidates=retrieval_settings.candidates,
            candidate_k=retrieval_settings.candidate_k,
            top_k=retrieval_settings.top_k,
            reranker=reranker,
        ) as retrieval:
            return await run_end_to_end(
                golden=golden,
                spec=spec,
                router=router,
                tools=search_docs_registry(retrieval),
                judge=judge,
                pacer=pacer,
                results_dir=args.results_dir,
                top_k=retrieval_settings.top_k,
                system=config.agent.system,
                max_tokens=config.agent.max_tokens,
                fresh=args.fresh,
                max_consecutive_failures=settings.max_consecutive_failures,
            )

    try:
        results = asyncio.run(run())
    except ResumeError as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    done = sum(not r.failed for r in results.records)
    failed = len(results.records) - done
    print(
        f"{args.results_dir}: row {name}: {done} of {results.questions_total} answered, "
        f"{failed} failed; {pacer.requests} requests in this run"
    )
    if results.stopped or len(results.records) < results.questions_total:
        print(f"row {name} is incomplete: {results.stopped or 'questions not yet run'}")
        return 1
    return 0
