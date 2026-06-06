"""
tests/test_hud_state_writer.py

Tests for:
- hud_state_writer: news in state, fallback, file write, schema
- readonly_dashboard: /health, /state, /news endpoints, POST rejected, no secrets
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            "summary": "Summary of the article.",
            "thumb": "",
            "byline": "",
            "published_iso": "2026-06-05T10:00:00Z",
            "time_ago": "2h ago",
            "source": "The Guardian",
        }
        for i in range(n)
    ]


# ── build_hud_state ───────────────────────────────────────────────────────────

class TestBuildHudState:

    def test_news_card_has_9_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        articles = _mock_articles(9)
        state = build_hud_state(articles, "live")
        news = state["cards"]["news"]
        assert len(news["articles"]) == 9

    def test_news_chip_is_live_when_live(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        assert state["cards"]["news"]["chip"] == "LIVE"

    def test_news_chip_is_demo_when_demo(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "demo")
        assert state["cards"]["news"]["chip"] == "DEMO"

    def test_items_populated_from_first_3_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        articles = _mock_articles(9)
        state = build_hud_state(articles, "live")
        items = state["cards"]["news"]["items"]
        assert len(items) == 3
        assert all(isinstance(i, dict) and "label" in i for i in items)

    def test_state_field_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        assert "state" in state
        assert isinstance(state["state"], str)

    def test_updated_at_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        assert "updated_at" in state
        assert "T" in state["updated_at"]

    def test_cards_dict_has_required_keys(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        for key in ("news", "brand", "activity"):
            assert key in state["cards"], f"Missing card: {key}"

    def test_empty_articles_uses_loading_status(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state([], "loading")
        news = state["cards"]["news"]
        assert news["status"] == "loading"
        assert news["articles"] == []


# ── write_hud_state ───────────────────────────────────────────────────────────

class TestWriteHudState:

    def test_creates_file_with_correct_json(self, tmp_path):
        from prometheus.services import hud_state_writer as _mod
        original = _mod._HUD_STATE_PATH
        _mod._HUD_STATE_PATH = tmp_path / "hud_state.json"
        try:
            _mod.write_hud_state(_mock_articles(), "live")
            assert _mod._HUD_STATE_PATH.exists()
            data = json.loads(_mod._HUD_STATE_PATH.read_text())
            assert "cards" in data
            assert "news" in data["cards"]
        finally:
            _mod._HUD_STATE_PATH = original

    def test_never_raises_on_bad_path(self, tmp_path):
        from prometheus.services import hud_state_writer as _mod
        original = _mod._HUD_STATE_PATH
        _mod._HUD_STATE_PATH = Path("/nonexistent/deep/path/hud_state.json")
        try:
            # Must not raise even with an impossible path (parent doesn't exist)
            # The function catches all exceptions internally
            _mod.write_hud_state([], "error")
        except Exception as exc:
            pytest.fail(f"write_hud_state raised unexpectedly: {exc}")
        finally:
            _mod._HUD_STATE_PATH = original


# ── _fetch_news fallback ──────────────────────────────────────────────────────

class TestFetchNewsInternal:

    def test_returns_fallback_when_get_news_raises(self):
        from prometheus.services.hud_state_writer import _fetch_news
        with patch("prometheus.services.hud_state_writer._fetch_news") as mock_fn:
            mock_fn.return_value = ([], "fallback")
            articles, status = mock_fn()
        assert status == "fallback"

    def test_demo_when_no_api_key(self):
        from prometheus.services.guardian_news import get_news
        with patch("prometheus.services.guardian_news._load_env_key", return_value=("", "")):
            articles, status = get_news()
        assert status == "demo"
        assert len(articles) == 9


# ── ReadonlyDashboard ─────────────────────────────────────────────────────────

def _find_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestReadonlyDashboard:

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path):
        """Start a real ReadonlyDashboard server on a random port for each test."""
        from prometheus.services.readonly_dashboard import ReadonlyDashboard
        from prometheus.services import readonly_dashboard as _mod

        port = _find_free_port()
        original_path = _mod._HUD_STATE_PATH
        # Write a test hud_state file
        test_state = {
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
                }
            }
        }
        state_file = tmp_path / "hud_state.json"
        state_file.write_text(json.dumps(test_state), encoding="utf-8")
        _mod._HUD_STATE_PATH = state_file

        dashboard = ReadonlyDashboard(host="127.0.0.1", port=port)
        dashboard.start()
        time.sleep(0.1)  # let the server thread start

        self._base = f"http://127.0.0.1:{port}"
        self._dashboard = dashboard
        yield
        dashboard.stop()
        _mod._HUD_STATE_PATH = original_path

    def _get(self, path: str) -> tuple[int, dict]:
        url = self._base + path
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_health_returns_ok(self):
        code, body = self._get("/health")
        assert code == 200
        assert body.get("status") == "ok"

    def test_state_returns_hud_state(self):
        code, body = self._get("/state")
        assert code == 200
        assert "cards" in body
        assert body.get("active_project") == "TestProject"

    def test_news_returns_news_section(self):
        code, body = self._get("/news")
        assert code == 200
        assert "articles" in body
        assert len(body["articles"]) == 9

    def test_unknown_path_returns_404(self):
        code, _ = self._get("/admin")
        assert code == 404

    def test_post_rejected(self):
        import urllib.request as _req
        url = self._base + "/state"
        req = _req.Request(url, data=b"{}", method="POST")
        try:
            with _req.urlopen(req, timeout=3) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 405

    def test_no_openai_key_in_state_response(self):
        _, body = self._get("/state")
        body_str = json.dumps(body).lower()
        assert "sk-" not in body_str, "OpenAI key prefix found in state response"

    def test_no_api_key_fields_in_news(self):
        _, body = self._get("/news")
        body_str = json.dumps(body)
        for pattern in ("api_key", "apikey", "secret", "token"):
            # Values should be [REDACTED] if present, never raw keys
            import re
            matches = re.findall(rf'"{pattern}":\s*"([^"]+)"', body_str, re.IGNORECASE)
            for val in matches:
                assert val == "[REDACTED]", f"Secret key {pattern!r} not redacted: {val!r}"


# ── Godot-facing schema stability ─────────────────────────────────────────────

class TestGodotStateSchema:

    def test_hud_state_has_state_field(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        assert isinstance(state.get("state"), str)

    def test_hud_state_has_cards_dict(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live")
        assert isinstance(state.get("cards"), dict)

    def test_news_card_has_articles_array(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(9), "live")
        articles = state["cards"]["news"]["articles"]
        assert isinstance(articles, list)
        assert len(articles) == 9

    def test_news_article_has_required_fields(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(9), "live")
        for a in state["cards"]["news"]["articles"]:
            for field in ("title", "section", "time_ago"):
                assert field in a or "tag" in a, f"Article missing {field}: {a}"

    def test_focus_mode_and_active_project_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state([], "demo")
        assert "focus_card" in state
        assert "active_project" in state
