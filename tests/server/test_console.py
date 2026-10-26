"""The operator console page and its vendored, pinned scripts."""

from fastapi.testclient import TestClient

from vessel_mind.server.app import create_app


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


def test_the_console_listens_for_every_protocol_event_it_renders():
    # No browser runs in CI; this checks the SSE subscription names the tool events.
    with TestClient(create_app(SilentModel())) as client:
        script = client.get("/static/console.js").text

    assert 'sse-swap", "text,tool_start,proposal,tool_result,done,error"' in script


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
            "/static/console.js",
        ]
        for src in scripts:
            assert f'src="{src}"' in page
            assert client.get(src).status_code == 200

    assert "://" not in page  # nothing loads from another origin
