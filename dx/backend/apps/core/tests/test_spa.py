from pathlib import Path

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

from config.static import vite_immutable_file


def test_spa_reports_missing_build(client: Client, settings: Settings, tmp_path: Path) -> None:
    settings.SPA_INDEX = tmp_path / "index.html"

    response = client.get("/some/client/route")

    assert response.status_code == 503


def test_spa_serves_index_for_deep_links(
    client: Client, settings: Settings, tmp_path: Path
) -> None:
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><div id='root'></div>")
    settings.SPA_INDEX = index

    response = client.get("/datasets/42")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-cache"
    assert b"id='root'" in response.content


@pytest.mark.parametrize(
    ("url", "immutable"),
    [
        ("/static/assets/index-D2Ka7Pkg.js", True),
        ("/static/assets/geist-latin-wght-normal-BgDaEnEv.woff2", True),
        ("/static/index.html", False),
        ("/static/admin/css/base.css", False),
    ],
)
def test_vite_hashed_assets_are_immutable(url: str, immutable: bool) -> None:
    assert vite_immutable_file("", url) is immutable
