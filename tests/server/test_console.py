"""The operator console page and its vendored, pinned scripts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vessel_mind.server.app import create_app

CITATIONS_JS = Path(__file__).resolve().parents[2] / "src/vessel_mind/server/static/citations.js"


class SilentModel:
    id = "silent"

    async def stream(self, req):
        return
        yield


def test_the_console_page_has_a_question_box_and_a_stop_button():
    with TestClient(create_app(SilentModel())) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'id="question"' in page.text
    assert 'id="stop"' in page.text


FREE_TIER_NOTICE = (
    "Messages are sent to Google Gemini's free tier, which may use them to improve its "
    "models. Do not enter personal data."
)


def notice_text(page: str) -> str:
    return " ".join(page.split())


def test_the_public_host_shows_the_free_tier_notice():
    with TestClient(create_app(SilentModel(), public_host=True, demo_mode=True)) as client:
        page = client.get("/").text

    assert FREE_TIER_NOTICE in notice_text(page)
    assert 'id="free-tier"' in page


def test_localhost_does_not_show_the_free_tier_notice():
    with TestClient(create_app(SilentModel())) as client:
        page = client.get("/").text

    assert "free tier" not in page
    assert 'id="free-tier"' not in page


def test_the_console_listens_for_every_protocol_event_it_renders():
    # No browser runs in CI; this checks the SSE subscription names the tool events.
    with TestClient(create_app(SilentModel())) as client:
        script = client.get("/static/console.js").text

    assert 'sse-swap", "route,text,tool_start,proposal,tool_result,done,error"' in script
    assert 'event.type === "route"' in script


def test_the_console_resolves_proposals_only_through_the_protocol():
    with TestClient(create_app(SilentModel())) as client:
        script = client.get("/static/console.js").text

    assert "/proposals/${encodeURIComponent(proposalId)}/${decision}" in script
    assert '"approve"' in script
    assert '"reject"' in script


def test_the_console_scripts_are_served_from_the_app_not_a_cdn():
    with TestClient(create_app(SilentModel())) as client:
        page = client.get("/").text
        scripts = [
            "/static/vendor/htmx-2.0.11.min.js",
            "/static/vendor/htmx-ext-sse-2.2.4.min.js",
            "/static/citations.js",
            "/static/route.js",
            "/static/console.js",
        ]
        for src in scripts:
            assert f'src="{src}"' in page
            assert client.get(src).status_code == 200

    assert "://" not in page  # nothing loads from another origin


def test_the_console_maps_citations_from_search_docs_results():
    with TestClient(create_app(SilentModel())) as client:
        script = client.get("/static/console.js").text

    assert 'event.name === "search_docs"' in script
    assert "VesselCitations.split" in script


def split_citations(text: str, results: list[dict]) -> list:
    """Runs citations.js's split in node: the console's own code, outside a browser."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = (
        "const vm = require('vm'); const fs = require('fs');"
        "const window = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window});"
        "const [text, results] = JSON.parse(process.argv[2]);"
        "const map = window.VesselCitations.fromResults(results);"
        "console.log(JSON.stringify(window.VesselCitations.split(text, map)));"
    )
    out = subprocess.run(  # noqa: S603 -- fixed program, arguments are data
        [node, "-e", program, str(CITATIONS_JS), json.dumps([text, results])],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


RESULTS = [
    {
        "n": 1,
        "source": "vessel-mind/GLOSSARY.md#proposal",
        "url": "https://github.com/o/r/blob/c/GLOSSARY.md#proposal",
    },
    {"n": 2, "source": "vibeconform/README.md", "url": "https://github.com/o/v/blob/c/README.md"},
]


def test_a_citation_becomes_a_link_to_its_source_and_unknown_numbers_stay_text():
    parts = split_citations("Waits [1]. Both [1][2], or [2, 1]. Not [9].", RESULTS)

    p1 = {"n": 1, "source": RESULTS[0]["source"], "url": RESULTS[0]["url"]}
    p2 = {"n": 2, "source": RESULTS[1]["source"], "url": RESULTS[1]["url"]}
    assert parts == ["Waits ", p1, ". Both ", p1, p2, ", or ", p2, p1, ". Not [9]."]


def test_a_url_that_is_not_github_is_never_a_link():
    parts = split_citations("See [1].", [{"n": 1, "source": "x", "url": "javascript:alert(1)"}])

    assert parts == ["See [1]."]


ROUTE_JS = CITATIONS_JS.with_name("route.js")


def route_badge(route: dict, done: dict | None) -> str:
    """Runs route.js's describe in node: the console's own badge text."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    program = (
        "const vm = require('vm'); const fs = require('fs');"
        "const window = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window});"
        "const [route, done] = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(window.VesselRoute.describe(route, done)));"
    )
    out = subprocess.run(  # noqa: S603 -- fixed program, arguments are data
        [node, "-e", program, str(ROUTE_JS), json.dumps([route, done])],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


USAGE = {"input_tokens": 200, "output_tokens": 12, "thinking_tokens": 0}


def test_the_route_badge_names_the_route_the_models_and_both_usages():
    route = {
        "type": "route",
        "route": "fast",
        "reason": "classified",
        "router_model": "small",
        "router_usage": {"input_tokens": 50, "output_tokens": 6, "thinking_tokens": 0},
        "model": "small",
    }

    badge = route_badge(route, {"type": "done", "reason": "end", "usage": USAGE, "steps": 1})

    assert badge == (
        "fast path (classified) · router small: in 50, out 6, thinking 0"
        " · answer small: in 200, out 12, thinking 0"
    )


def test_the_route_badge_with_the_router_off_names_only_the_answering_model():
    route = {
        "type": "route",
        "route": "slow",
        "reason": "router_off",
        "router_model": None,
        "router_usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0},
        "model": "large",
    }

    badge = route_badge(route, None)

    assert badge == "slow path (router_off) · answer large"
