"""The corpus: public sources, each pinned to one commit (docs/specs/a-agent-track.md §1).

A source is read only at its pin. A ``git-archive`` source is read from this
clone with ``git archive``, so it needs no token while the repository is
private. A ``tarball`` source is the GitHub archive of the commit, checked
against its SHA-256 and cached. Anything that cannot be pinned raises
CorpusPinError, and callers (ingest, eval) refuse to run.
"""

import hashlib
import io
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class CorpusPinError(Exception):
    """A source cannot be read at a verified pin."""


class ChecksumMismatchError(CorpusPinError):
    """A tarball's SHA-256 differs from the one in the corpus file."""


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    fetch: Literal["git-archive", "tarball"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    include: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _tarball_needs_sha256(self) -> Self:
        if self.fetch == "tarball" and self.sha256 is None:
            raise ValueError(f"source {self.name!r}: a tarball source needs sha256")
        return self

    @property
    def tarball_url(self) -> str:
        return f"https://github.com/{self.repo}/archive/{self.commit}.tar.gz"

    def includes(self, path: str) -> bool:
        return any(PurePosixPath(path).full_match(pattern) for pattern in self.include)


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[Source, ...] = Field(min_length=1)


@dataclass(frozen=True)
class Document:
    """One corpus file at its source's pin. ``path`` is relative to the source's root."""

    source_name: str
    repo: str
    commit: str
    path: str
    text: str


Fetch = Callable[[str], bytes]


def load_corpus(path: Path) -> Corpus:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Corpus.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise CorpusPinError(f"{path}: {exc}") from exc


def http_fetch(url: str) -> bytes:
    response = httpx.get(url, follow_redirects=True, timeout=120)
    response.raise_for_status()
    return response.content


def read_corpus(
    corpus: Corpus, *, repo_root: Path, cache_dir: Path, fetch: Fetch = http_fetch
) -> list[Document]:
    """Read every included file of every source at its pin, or raise CorpusPinError."""
    documents: list[Document] = []
    for source in corpus.sources:
        if source.fetch == "git-archive":
            archive = _git_archive(source, repo_root)
            strip_prefix = False
        else:
            archive = _cached_tarball(source, cache_dir, fetch)
            strip_prefix = True
        documents.extend(_documents(source, archive, strip_prefix=strip_prefix))
    return documents


def _git_archive(source: Source, repo_root: Path) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise CorpusPinError(f"source {source.name!r}: git is not installed")
    try:
        # S603: no shell, and the commit is validated as 40 hex characters.
        # core.autocrlf=false: git archive applies it, and a pinned commit must give
        # the same bytes (and hashes) on every machine.
        return subprocess.run(  # noqa: S603
            [
                git,
                "-C",
                str(repo_root),
                "-c",
                "core.autocrlf=false",
                "archive",
                "--format=tar",
                source.commit,
            ],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise CorpusPinError(
            f"source {source.name!r}: cannot read commit {source.commit} from {repo_root}: "
            f"{detail.decode(errors='replace').strip() or exc}"
        ) from exc


def _cached_tarball(source: Source, cache_dir: Path, fetch: Fetch) -> bytes:
    cached = cache_dir / f"{source.name}-{source.commit}.tar.gz"
    if cached.exists():
        data = cached.read_bytes()
        _check_sha256(source, data, origin=str(cached))
        return data
    try:
        data = fetch(source.tarball_url)
    except Exception as exc:
        raise CorpusPinError(
            f"source {source.name!r}: cannot fetch {source.tarball_url}: {exc}"
        ) from exc
    _check_sha256(source, data, origin=source.tarball_url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = cached.with_suffix(".partial")
    partial.write_bytes(data)
    partial.replace(cached)
    return data


def _check_sha256(source: Source, data: bytes, *, origin: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != source.sha256:
        raise ChecksumMismatchError(
            f"source {source.name!r}: {origin} has SHA-256 {actual}, "
            f"the corpus file pins {source.sha256}"
        )


def _documents(source: Source, archive: bytes, *, strip_prefix: bool) -> list[Document]:
    documents: list[Document] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if not member.isfile():
                    continue
                parts = PurePosixPath(member.name).parts
                if strip_prefix:
                    parts = parts[1:]
                if not parts:
                    continue
                path = PurePosixPath(*parts).as_posix()
                if not source.includes(path):
                    continue
                stream = tar.extractfile(member)
                if stream is None:
                    continue
                documents.append(
                    Document(
                        source_name=source.name,
                        repo=source.repo,
                        commit=source.commit,
                        path=path,
                        text=stream.read().decode("utf-8"),
                    )
                )
    except (tarfile.TarError, UnicodeDecodeError) as exc:
        raise CorpusPinError(f"source {source.name!r}: unreadable archive: {exc}") from exc
    documents.sort(key=lambda doc: doc.path)
    return documents
