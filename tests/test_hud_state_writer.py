"""
tests/test_hud_state_writer.py

Tests for:
- hud_state_writer: canonical path, news in state, fallback, file write, schema
- readonly_dashboard: /health, /state, /news, HTML page, POST rejected, no secrets
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_articles(n: int = 9) -> list[dict]:
    return [
        {
            "id": f"art-{i}",
            "title": f"Article {i} about AI and technology",
            "href": f"https://guardian.com/{i}",
            "tag": "Technology",
            "section": "Technology",
            "summary": f"Summary {i} of the article about AI.",
            "thumb": "",
            "byline": "",
            "published_iso": "2026-06-05T10:00:00Z",
            "time_ago": "2h ago",
            "source": "The Guardian",
        }
        for i in range(n)
    ]


def _find_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Canonical path ────────────────────────────────────────────────────────────

class TestCanonicalPath:

    def test_canonical_path_is_under_desktop_prometheus_state(self):
        from prometheus.services.hud_state_writer import _DASHBOARD_STATE_PATH
        assert "Desktop" in str(_DASHBOARD_STATE_PATH)
        assert "PROMETHEUS" in str(_DASHBOARD_STATE_PATH)
        assert "state" in str(_DASHBOARD_STATE_PATH)
        assert "dashboard_state.json" in str(_DASHBOARD_STATE_PATH)

    def test_readonly_dashboard_uses_same_canonical_path(self):
        from prometheus.services.hud_state_writer import _DASHBOARD_STATE_PATH
        from prometheus.services.readonly_dashboard import _DASHBOARD_STATE_PATH as _ro_path
        assert str(_DASHBOARD_STATE_PATH) == str(_ro_path)

    def test_write_dashboard_state_creates_correct_file(self, tmp_path):
        import prometheus.services.hud_state_writer as _mod
        original = _mod._DASHBOARD_STATE_PATH
        _mod._DASHBOARD_STATE_PATH = tmp_path / "state" / "dashboard_state.json"
        try:
            _mod.write_dashboard_state(_mock_articles(), "live")
            assert _mod._DASHBOARD_STATE_PATH.exists(), "canonical file must be created"
            data = json.loads(_mod._DASHBOARD_STATE_PATH.read_text())
            assert "cards" in data
            assert "news" in data["cards"]
        finally:
            _mod._DASHBOARD_STATE_PATH = original


# ── build_hud_state ───────────────────────────────────────────────────────────

class TestBuildHudState:

    def test_news_card_has_9_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(9), "live")
        assert len(state["cards"]["news"]["articles"]) == 9

    def test_news_chip_live(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert build_hud_state(_mock_articles(), "live")["cards"]["news"]["chip"] == "LIVE"

    def test_news_chip_demo(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert build_hud_state(_mock_articles(), "demo")["cards"]["news"]["chip"] == "DEMO"

    def test_items_populated_from_first_3_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        items = build_hud_state(_mock_articles(9), "live")["cards"]["news"]["items"]
        assert len(items) == 3
        assert all(isinstance(i, dict) and "label" in i for i in items)

    def test_state_field_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert isinstance(build_hud_state(_mock_articles(), "live").get("state"), str)

    def test_updated_at_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert "T" in build_hud_state(_mock_articles(), "live")["updated_at"]

    def test_required_cards_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        cards = build_hud_state(_mock_articles(), "live")["cards"]
        for key in ("news", "brand", "activity", "objective"):
            assert key in cards, f"Missing card: {key}"

    def test_loading_state_has_empty_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state([], "loading")
        assert state["cards"]["news"]["articles"] == []
        assert state["cards"]["news"]["status"] == "loading"

    def test_focus_card_and_mode_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state([], "demo")
        assert "focus_card" in state
        assert "mode" in state
        assert "active_project" in state


# ── write_dashboard_state ─────────────────────────────────────────────────────

class TestWriteDashboardState:

    def test_creates_file_atomically(self, tmp_path):
        import prometheus.services.hud_state_writer as _mod
        original = _mod._DASHBOARD_STATE_PATH
        _mod._DASHBOARD_STATE_PATH = tmp_path / "dashboard_state.json"
        try:
            _mod.write_dashboard_state(_mock_articles(), "live")
            assert _mod._DASHBOARD_STATE_PATH.exists()
            data = json.loads(_mod._DASHBOARD_STATE_PATH.read_text())
            assert "cards" in data
        finally:
            _mod._DASHBOARD_STATE_PATH = original

    def test_never_raises_on_impossible_path(self):
        import prometheus.services.hud_state_writer as _mod
        original = _mod._DASHBOARD_STATE_PATH
        _mod._DASHBOARD_STATE_PATH = Path("/nonexistent/deep/path/dashboard_state.json")
        try:
            _mod.write_dashboard_state([], "error")  # must not raise
        except Exception as exc:
            pytest.fail(f"write_dashboard_state raised: {exc}")
        finally:
            _mod._DASHBOARD_STATE_PATH = original

    def test_news_count_in_written_file(self, tmp_path):
        import prometheus.services.hud_state_writer as _mod
        original = _mod._DASHBOARD_STATE_PATH
        _mod._DASHBOARD_STATE_PATH = tmp_path / "dashboard_state.json"
        try:
            _mod.write_dashboard_state(_mock_articles(9), "live")
            data = json.loads(_mod._DASHBOARD_STATE_PATH.read_text())
            assert len(data["cards"]["news"]["articles"]) == 9
        finally:
            _mod._DASHBOARD_STATE_PATH = original


# ── Guardian news fallback ────────────────────────────────────────────────────

class TestFetchNewsInternal:

    def test_fallback_when_no_api_key(self):
        from prometheus.services.guardian_news import get_news
        with patch("prometheus.services.guardian_news._load_env_key", return_value=("", "")):
            articles, status = get_news()
        assert status == "demo"
        assert len(articles) == 9

    def test_fetch_news_returns_fallback_on_network_error(self):
        from prometheus.services.hud_state_writer import _fetch_news
        with patch("prometheus.services.guardian_news.fetch_guardian_articles",
                   side_effect=RuntimeError("network error")), \
             patch("prometheus.services.guardian_news._load_env_key",
                   return_value=("some-key", "https://api")):
            articles, status = _fetch_news()
        assert status == "fallback"
        assert len(articles) == 9


# ── ReadonlyDashboard ─────────────────────────────────────────────────────────

class TestReadonlyDashboard:

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path):
        from prometheus.services.readonly_dashboard import ReadonlyDashboard
        import prometheus.services.readonly_dashboard as _mod

        port = _find_free_port()
        original = _mod._DASHBOARD_STATE_PATH
        state_file = tmp_path / "dashboard_state.json"
        state_file.write_text(json.dumps({
            "state": "idle",
            "active_project": "TestProject",
            "updated_at": "2026-06-05T10:00:00Z",
            "cards": {
                "news": {
                    "title": "News",
                    "chip": "LIVE",
                    "status": "live",
                    "articles": _mock_articles(9),
                    "items": [],
                },
                "activity": {"title": "Activity", "chip": "LIVE", "items": ["event_a"], "summary": ""},
            }
        }), encoding="utf-8")
        _mod._DASHBOARD_STATE_PATH = state_file

        self._dashboard = ReadonlyDashboard(host="127.0.0.1", port=port)
        self._dashboard.start()
        time.sleep(0.15)
        self._base = f"http://127.0.0.1:{port}"
        yield
        self._dashboard.stop()
        _mod._DASHBOARD_STATE_PATH = original

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self._base + path, timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_health_returns_ok(self):
        code, body = self._get("/health")
        assert code == 200
        assert json.loads(body)["status"] == "ok"

    def test_state_has_cards_and_project(self):
        code, body = self._get("/state")
        assert code == 200
        d = json.loads(body)
        assert "cards" in d
        assert d.get("active_project") == "TestProject"

    def test_news_has_9_articles(self):
        code, body = self._get("/news")
        assert code == 200
        assert len(json.loads(body)["articles"]) == 9

    def test_root_html_is_html(self):
        code, body = self._get("/")
        assert code == 200
        assert b"<html" in body.lower()

    def test_root_html_includes_news_title(self):
        code, body = self._get("/")
        assert code == 200
        # All 9 article titles should appear somewhere in the HTML
        assert b"Article 0" in body

    def test_root_html_shows_project(self):
        _, body = self._get("/")
        assert b"TestProject" in body

    def test_unknown_path_404(self):
        code, _ = self._get("/admin")
        assert code == 404

    def test_post_rejected_405(self):
        req = urllib.request.Request(
            self._base + "/state", data=b"{}", method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 405

    def test_no_secrets_in_state_response(self):
        _, body = self._get("/state")
        text = body.decode().lower()
        assert "sk-" not in text

    def test_api_key_values_redacted(self):
        import prometheus.services.readonly_dashboard as _mod
        original = _mod._DASHBOARD_STATE_PATH

        import tempfile, json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump({"state": "idle", "vite_guardian_api_key": "secret-key-123"}, f)
            tmp_path = f.name

        _mod._DASHBOARD_STATE_PATH = Path(tmp_path)
        try:
            _, body = self._get("/state")
            assert b"secret-key-123" not in body
            assert b"REDACTED" in body
        finally:
            _mod._DASHBOARD_STATE_PATH = original
            os.unlink(tmp_path)


# ── Godot-facing schema stability ─────────────────────────────────────────────

class TestGodotStateSchema:

    def test_state_field_is_string(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert isinstance(build_hud_state([], "demo")["state"], str)

    def test_cards_is_dict(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert isinstance(build_hud_state([], "demo")["cards"], dict)

    def test_news_articles_is_list(self):
        from prometheus.services.hud_state_writer import build_hud_state
        articles = build_hud_state(_mock_articles(9), "live")["cards"]["news"]["articles"]
        assert isinstance(articles, list)
        assert len(articles) == 9

    def test_each_article_has_title_and_section(self):
        from prometheus.services.hud_state_writer import build_hud_state
        for a in build_hud_state(_mock_articles(9), "live")["cards"]["news"]["articles"]:
            assert "title" in a
            assert ("section" in a or "tag" in a)

    def test_news_items_fallback_is_list_of_dicts(self):
        from prometheus.services.hud_state_writer import build_hud_state
        items = build_hud_state(_mock_articles(9), "live")["cards"]["news"]["items"]
        assert isinstance(items, list)
        assert all(isinstance(i, dict) for i in items)

    def test_articles_preserve_thumb_field(self):
        from prometheus.services.hud_state_writer import build_hud_state
        articles_in = _mock_articles(9)
        articles_in[0]["thumb"] = "https://media.guim.co.uk/thumb.jpg"
        articles_out = build_hud_state(articles_in, "live")["cards"]["news"]["articles"]
        assert articles_out[0]["thumb"] == "https://media.guim.co.uk/thumb.jpg"


# ── Thumbnail end-to-end ──────────────────────────────────────────────────────

def _mock_articles_with_thumb(n: int = 9) -> list[dict]:
    articles = _mock_articles(n)
    articles[0]["thumb"] = "https://media.guim.co.uk/sample/500.jpg"
    articles[0]["thumbnail"] = "https://media.guim.co.uk/sample/500.jpg"
    return articles


class TestThumbnailEndToEnd:

    @pytest.fixture(autouse=True)
    def _server_with_thumb(self, tmp_path):
        from prometheus.services.readonly_dashboard import ReadonlyDashboard
        import prometheus.services.readonly_dashboard as _mod

        port = _find_free_port()
        original = _mod._DASHBOARD_STATE_PATH
        state_file = tmp_path / "dashboard_state.json"
        state_file.write_text(json.dumps({
            "state": "idle",
            "active_project": "ThumbTest",
            "updated_at": "2026-06-06T00:00:00Z",
            "cards": {
                "news": {
                    "title": "News",
                    "chip": "LIVE",
                    "status": "live",
                    "articles": _mock_articles_with_thumb(9),
                    "items": [],
                },
            },
        }), encoding="utf-8")
        _mod._DASHBOARD_STATE_PATH = state_file

        self._dashboard = ReadonlyDashboard(host="127.0.0.1", port=port)
        self._dashboard.start()
        import time
        time.sleep(0.15)
        self._base = f"http://127.0.0.1:{port}"
        yield
        self._dashboard.stop()
        _mod._DASHBOARD_STATE_PATH = original

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(self._base + path, timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_news_endpoint_exposes_thumb_field(self):
        code, body = self._get("/news")
        assert code == 200
        data = json.loads(body)
        articles = data.get("articles", [])
        assert len(articles) == 9
        # First article should have thumb URL
        assert articles[0].get("thumb") == "https://media.guim.co.uk/sample/500.jpg"

    def test_html_includes_img_tag_when_thumb_present(self):
        _, body = self._get("/")
        assert b'<img' in body
        assert b'media.guim.co.uk/sample/500.jpg' in body

    def test_html_no_broken_img_when_thumb_missing(self):
        _, body = self._get("/")
        # Articles without thumb should not have onerror or empty src img tags
        html = body.decode()
        # Count articles rendered — we only have 1 with thumb, 8 without
        # The 8 articles without thumb should not produce <img src=""> broken images
        import re
        empty_src_imgs = re.findall(r'<img[^>]+src=""', html)
        assert empty_src_imgs == [], f"Found empty-src img tags: {empty_src_imgs}"

    def test_no_secrets_in_html(self):
        _, body = self._get("/")
        text = body.decode().lower()
        assert "sk-" not in text
        assert "api_key" not in text or "[redacted]" in text or "read-only" in text
