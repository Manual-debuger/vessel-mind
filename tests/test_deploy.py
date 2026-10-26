"""The deployment files: compose.yaml, deploy/Dockerfile and .dockerignore (ADR 0002).

These read the files; they do not run Docker. The image itself is checked by
building it and running `docker compose up` (see the PR).
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "compose.yaml"
DOCKERFILE = REPO / "deploy" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def environment(service: dict) -> dict[str, str | None]:
    """A service's environment as a mapping; a bare NAME (pass-through) maps to None."""
    env = service.get("environment", {})
    if isinstance(env, dict):
        return env
    return {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else None for item in env}


# --- The tunnel token -------------------------------------------------------------

# A tunnel token is base64 of a JSON object that starts {"a":"<account tag>", ...}.
# The prefix is computed so that this file does not hold it either.
TOKEN_PREFIX = base64.b64encode(b'{"a":"').decode()
TOKEN = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9+/=_-]{20,}")
# TUNNEL_TOKEN=value or TUNNEL_TOKEN: value, where the value looks like a secret
# (not a ${reference} or a "..." placeholder in a comment).
TOKEN_ASSIGNED = re.compile(r"TUNNEL_TOKEN\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{20,}")


def tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    out = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [REPO / name for name in out.decode().split("\0") if name]


def test_no_committed_file_holds_a_tunnel_token():
    found = []
    for path in tracked_files():
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        if TOKEN.search(text) or TOKEN_ASSIGNED.search(text):
            found.append(path.relative_to(REPO).as_posix())

    assert found == []


def test_the_token_check_finds_a_token_and_an_assignment():
    # The check above must not pass because it can never match.
    fake = base64.b64encode(b'{"a":"0123456789abcdef","t":"x","s":"y"}').decode()

    assert TOKEN.search(f"command: tunnel run --token {fake}")
    value = "a1" * 12
    assert TOKEN_ASSIGNED.search(f"TUNNEL_TOKEN={value}")
    assert TOKEN_ASSIGNED.search(f'  TUNNEL_TOKEN: "{value}"')
    assert not TOKEN_ASSIGNED.search("  - TUNNEL_TOKEN")
    assert not TOKEN_ASSIGNED.search("  TUNNEL_TOKEN: ${TUNNEL_TOKEN}")
    assert not TOKEN_ASSIGNED.search("#   TUNNEL_TOKEN=... docker compose up")


def test_the_env_file_compose_reads_is_git_ignored():
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    ignored = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "-C", str(REPO), "check-ignore", "-q", ".env"], check=False
    )

    assert ignored.returncode == 0


def test_cloudflared_takes_its_token_from_the_environment_only():
    tunnel = compose()["services"]["cloudflared"]

    # A bare name: compose passes the shell's value through, and no file holds one.
    assert environment(tunnel) == {"TUNNEL_TOKEN": None}
    assert "--token" not in " ".join(tunnel.get("command", []))


# --- What the tunnel can reach -----------------------------------------------------


def test_a_plain_compose_up_starts_only_the_app_and_the_tunnel_needs_its_profile():
    services = compose()["services"]

    assert [name for name, s in services.items() if "profiles" not in s] == ["app"]
    assert services["cloudflared"]["profiles"] == ["tunnel"]


def test_only_the_app_listens_and_only_on_the_loopback_interface():
    services = compose()["services"]

    assert all(port.startswith("127.0.0.1:") for port in services["app"]["ports"])
    assert "ports" not in services["cloudflared"]
    assert "network_mode" not in services["cloudflared"]


def test_the_app_needs_only_the_gemini_key_and_the_tunnel_token_never_reaches_it():
    env = environment(compose()["services"]["app"])

    assert env["GEMINI_API_KEY"] is None  # passed through from the shell
    assert "TUNNEL_TOKEN" not in env


def test_the_state_file_is_in_a_volume_apart_from_the_index_the_image_ships():
    app = compose()["services"]["app"]

    assert "state:/app/data/state" in app["volumes"]
    assert not any(v.split(":")[1] in ("/app/data", "/app") for v in app["volumes"])


# --- The image ---------------------------------------------------------------------


def stages() -> dict[str, list[str]]:
    """Each build stage's instructions, joined across line continuations."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    result: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"FROM\s+\S+\s+AS\s+(\S+)", line, re.IGNORECASE)
        if match:
            current = match.group(1)
            result[current] = []
        if current is not None:
            result[current].append(line)
    return result


def test_the_node_stage_installs_the_notion_server_from_the_lockfile():
    node = stages()["notion-mcp"]

    assert node[0].split()[1].startswith("node:")
    assert any("package-lock.json" in line for line in node if line.startswith("COPY"))
    assert any(re.search(r"\bnpm ci\b", line) for line in node if line.startswith("RUN"))


def test_nothing_in_the_image_or_compose_runs_npx_or_npm_install():
    instructions = [line for lines in stages().values() for line in lines]
    compose_text = COMPOSE.read_text(encoding="utf-8")

    for line in [*instructions, compose_text]:
        assert not re.search(r"\bnpx\b", line), line
        assert not re.search(r"\bnpm (install|i)\b", line), line


def test_the_runtime_stage_is_last_and_never_holds_npm_or_the_git_history():
    all_stages = stages()
    name, runtime = list(all_stages.items())[-1]

    assert name == "runtime"
    assert not any(".git" in line.split() or "/.git" in line for line in runtime)
    assert not any(re.search(r"\bnpm\b", line) for line in runtime)
    # node is copied in as one binary, to start the pinned server.
    assert any("/usr/local/bin/node" in line for line in runtime if line.startswith("COPY"))


def test_only_the_index_stage_reads_the_git_history_through_a_bind_mount():
    readers = [
        name
        for name, lines in stages().items()
        if any(re.search(r"(^|[\s=,/])\.git\b", line) for line in lines)
    ]

    assert readers == ["index"]
    assert any("--mount=type=bind" in line and ".git" in line for line in stages()["index"])


def test_the_index_stage_ingests_the_pinned_corpus_and_the_runtime_ships_it():
    index = " ".join(stages()["index"])
    runtime = " ".join(stages()["runtime"])

    assert "vessel_mind.rag.ingest" in index
    # The cpu reranker downloads its model on first use; the image ships it instead.
    assert "FastEmbedCrossEncoder" in index
    assert "--from=index" in runtime
    assert "data/index.sqlite" in runtime
    assert ".cache/fastembed" in runtime


def test_the_build_context_keeps_git_and_drops_local_secrets_and_data():
    ignored = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert ".git" not in ignored  # the index stage reads the corpus pin from it
    assert {".env", "config/agent.yaml", "data", ".cache", ".venv"} <= ignored
    assert "deploy/notion-mcp/node_modules" in ignored
