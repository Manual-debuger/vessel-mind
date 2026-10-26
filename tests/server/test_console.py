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
