"""``task eval``: the eval entry point (spec §5.8).

    task eval -- --retrieval-only [--config ... --index ... --model-cache ...]

The eval refuses to run (exit 2) when a corpus source cannot be pinned or an
expected source of the golden set is not in the corpus at its pin. Before it
measures, it brings the index up to the pinned corpus (an unchanged index is a
no-op), so the numbers are for the pinned documents. Settings come from the
server's config (retrieval:), so the eval measures what the server runs; the
flags override paths.
"""

import argparse
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from vessel_mind.eval.golden import GoldenSet, GoldenSetError, load_golden
from vessel_mind.eval.results import store_retrieval
from vessel_mind.eval.retrieval import Pin, RetrievalAblation, run_rows
from vessel_mind.rag.corpus import CorpusPinError, Document
from vessel_mind.rag.embed import Embedder, FastEmbedEmbedder
from vessel_mind.rag.ingest import corpus_loader, ingest
from vessel_mind.rag.rerank import CrossEncoderReranker, FastEmbedCrossEncoder
from vessel_mind.rag.search import Reranker
from vessel_mind.rag.store import IndexStore
from vessel_mind.server.config import ConfigError, load_config
from vessel_mind.server.main import DEFAULT_CONFIG, EXAMPLE_CONFIG

DEFAULT_GOLDEN = Path("eval/golden.yaml")
DEFAULT_RESULTS = Path("eval/results")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task eval", description="Run the eval.")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="table 1, the retrieval ablation; no LLM calls",
    )
    parser.add_argument("--config", type=Path, help="default: config/agent.yaml, else the example")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--corpus", type=Path, help="default: retrieval.corpus")
    parser.add_argument("--repo-root", type=Path, default=Path())
    parser.add_argument("--index", type=Path, help="default: retrieval.index")
    parser.add_argument("--tarball-cache", type=Path, help="default: retrieval.tarball_cache")
    parser.add_argument("--model-cache", type=Path, help="default: retrieval.model_cache")
    args = parser.parse_args(argv)

    if not args.retrieval_only:
        print(
            "eval refused: the end-to-end eval is not built yet; run with --retrieval-only",
            file=sys.stderr,
        )
        return 2
    config_path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    try:
        settings = load_config(config_path).retrieval
        golden = load_golden(args.golden)
    except (ConfigError, GoldenSetError) as exc:
        print(f"eval refused: {exc}", file=sys.stderr)
        return 2
    model_cache = args.model_cache or settings.model_cache

    def make_models() -> tuple[Embedder, Reranker]:
        embedder = FastEmbedEmbedder(settings.embedding_model, cache_dir=model_cache)
        encoder = FastEmbedCrossEncoder(settings.reranker_model, cache_dir=model_cache)
        return embedder, CrossEncoderReranker(encoder, max_candidates=settings.candidate_k)

    try:
        ablation = run_retrieval_only(
            golden=golden,
            load_documents=corpus_loader(
                args.corpus or settings.corpus,
                repo_root=args.repo_root,
                cache_dir=args.tarball_cache or settings.tarball_cache,
            ),
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
