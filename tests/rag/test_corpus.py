"""The pinned corpus: eval/corpus.yaml and how each source is read at its pin.

No test downloads anything: the tarball source gets a fake fetch, and the
git source reads a throwaway repository built in tmp_path.
"""

import gzip
import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from vessel_mind.rag.corpus import (
    ChecksumMismatchError,
    CorpusPinError,
    load_corpus,
    read_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_corpus_file_pins_both_sources_with_commits_and_globs():
    corpus = load_corpus(REPO_ROOT / "eval" / "corpus.yaml")

    by_name = {source.name: source for source in corpus.sources}
    assert set(by_name) == {"vessel-mind", "vibeconform"}

    vessel_mind = by_name["vessel-mind"]
    assert vessel_mind.repo == "Manual-debuger/vessel-mind"
    assert vessel_mind.fetch == "git-archive"
    assert set(vessel_mind.include) == {"docs/adr/*.md", "docs/specs/*.md", "GLOSSARY.md"}

    vibeconform = by_name["vibeconform"]
    assert vibeconform.repo == "Manual-debuger/VibeConform"
    assert vibeconform.fetch == "tarball"
    assert vibeconform.commit.startswith("384d89e")
    assert vibeconform.sha256 is not None
    assert set(vibeconform.include) == {"README.md", "docs/**/*.md"}

    for source in corpus.sources:
        assert len(source.commit) == 40


def _write_corpus(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _tarball(prefix: str, files: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILES = {
    "README.md": "# Fixture\n\nHello.\n",
    "docs/guide.md": "# Guide\n\nRead me.\n",
    "docs/deep/more.md": "# More\n\nDeeper.\n",
    "docs/notes.txt": "not markdown",
    "src/main.go": "package main",
}


def _tarball_corpus(tmp_path: Path, sha256: str) -> Path:
    return _write_corpus(
        tmp_path / "corpus.yaml",
        f"""
sources:
  - name: fixture
    repo: example/fixture
    commit: "{COMMIT}"
    fetch: tarball
    sha256: "{sha256}"
    include: [README.md, "docs/**/*.md"]
""",
    )


def test_a_tarball_source_is_read_at_its_pin_and_cached(tmp_path):
    tarball = _tarball(f"fixture-{COMMIT}", FILES)
    corpus = load_corpus(_tarball_corpus(tmp_path, hashlib.sha256(tarball).hexdigest()))
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return tarball

    cache = tmp_path / "cache"
    documents = read_corpus(corpus, repo_root=tmp_path, cache_dir=cache, fetch=fetch)

    assert fetched == [f"https://github.com/example/fixture/archive/{COMMIT}.tar.gz"]
    assert {doc.path for doc in documents} == {"README.md", "docs/guide.md", "docs/deep/more.md"}
    guide = next(doc for doc in documents if doc.path == "docs/guide.md")
    assert guide.source_name == "fixture"
    assert guide.repo == "example/fixture"
    assert guide.commit == COMMIT
    assert guide.text == "# Guide\n\nRead me.\n"

    def no_fetch(url: str) -> bytes:
        raise AssertionError("the cached tarball should be used")

    again = read_corpus(corpus, repo_root=tmp_path, cache_dir=cache, fetch=no_fetch)
    assert again == documents


def test_a_tarball_whose_sha256_does_not_match_is_refused_and_not_cached(tmp_path):
    tarball = _tarball(f"fixture-{COMMIT}", FILES)
    corpus = load_corpus(_tarball_corpus(tmp_path, "0" * 64))
    cache = tmp_path / "cache"

    with pytest.raises(ChecksumMismatchError):
        read_corpus(corpus, repo_root=tmp_path, cache_dir=cache, fetch=lambda url: tarball)

    assert not cache.exists() or not any(cache.iterdir())


def test_a_cached_tarball_that_was_altered_is_refused(tmp_path):
    tarball = _tarball(f"fixture-{COMMIT}", FILES)
    corpus = load_corpus(_tarball_corpus(tmp_path, hashlib.sha256(tarball).hexdigest()))
    cache = tmp_path / "cache"
    read_corpus(corpus, repo_root=tmp_path, cache_dir=cache, fetch=lambda url: tarball)
    for cached in cache.iterdir():
        cached.write_bytes(_tarball(f"fixture-{COMMIT}", {"README.md": "# Tampered\n"}))

    with pytest.raises(ChecksumMismatchError):
        read_corpus(corpus, repo_root=tmp_path, cache_dir=cache, fetch=lambda url: tarball)


def test_a_tarball_source_without_a_sha256_cannot_be_pinned(tmp_path):
    path = _write_corpus(
        tmp_path / "corpus.yaml",
        f"""
sources:
  - name: fixture
    repo: example/fixture
    commit: "{COMMIT}"
    fetch: tarball
    include: [README.md]
""",
    )

    with pytest.raises(CorpusPinError):
        load_corpus(path)


def test_a_short_commit_cannot_be_pinned(tmp_path):
    path = _write_corpus(
        tmp_path / "corpus.yaml",
        """
sources:
  - name: fixture
    repo: example/fixture
    commit: 384d89e
    fetch: git-archive
    include: [README.md]
""",
    )

    with pytest.raises(CorpusPinError):
        load_corpus(path)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None, "these tests build a git repository"
    # S603: no shell, and every argument is a literal of this test module.
    return subprocess.run(  # noqa: S603
        [git, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001-first.md").write_text("# First\n\nPinned.\n", encoding="utf-8")
    (repo / "GLOSSARY.md").write_text("# Glossary\n", encoding="utf-8")
    (repo / "notes.md").write_text("# Not included\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _git_corpus(tmp_path: Path, commit: str) -> Path:
    return _write_corpus(
        tmp_path / "corpus.yaml",
        f"""
sources:
  - name: self
    repo: example/self
    commit: "{commit}"
    fetch: git-archive
    include: ["docs/adr/*.md", GLOSSARY.md]
""",
    )


def test_a_git_source_is_read_at_its_pinned_commit_not_from_the_working_tree(tmp_path):
    repo, commit = _fixture_repo(tmp_path)
    (repo / "docs" / "adr" / "0001-first.md").write_text("# Edited later\n", encoding="utf-8")
    corpus = load_corpus(_git_corpus(tmp_path, commit))

    documents = read_corpus(corpus, repo_root=repo, cache_dir=tmp_path / "cache")

    assert {doc.path: doc.text for doc in documents} == {
        "docs/adr/0001-first.md": "# First\n\nPinned.\n",
        "GLOSSARY.md": "# Glossary\n",
    }
    assert all(doc.commit == commit for doc in documents)


def test_a_git_commit_missing_from_the_clone_cannot_be_pinned(tmp_path):
    repo, _ = _fixture_repo(tmp_path)
    corpus = load_corpus(_git_corpus(tmp_path, "f" * 40))

    with pytest.raises(CorpusPinError):
        read_corpus(corpus, repo_root=repo, cache_dir=tmp_path / "cache")
