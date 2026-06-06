"""
tests/test_guardian_news.py

Unit tests for prometheus.services.guardian_news.

All network calls are mocked — no real HTTP requests are made.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from prometheus.services.guardian_news import (
    normalize_article,
    prometheus_relevance_score,
    pad_to_nine,
    get_news,
    _fallback_articles,
    _time_ago,
)


# ── normalize_article ──────────────────────────────────────────────────────────

class TestNormalizeArticle:

    def test_extracts_standard_fields(self):
        raw = {
            "id": "technology/2026/ai-agents",
            "webTitle": "AI agents reshape software pipelines",
            "webUrl": "https://theguardian.com/technology/2026/ai",
            "sectionName": "Technology",
            "webPublicationDate": "2026-06-05T10:00:00Z",
            "fields": {
                "trailText": "A new wave of autonomous agents.",
                "thumbnail": "https://media.theguardian.com/thumb.jpg",
                "byline": "Jane Smith",
            },
        }
        a = normalize_article(raw)
        assert a["id"] == "technology/2026/ai-agents"
        assert a["title"] == "AI agents reshape software pipelines"
        assert a["href"] == "https://theguardian.com/technology/2026/ai"
        assert a["tag"] == "Technology"
        assert a["summary"] == "A new wave of autonomous agents."
        assert a["thumb"] == "https://media.theguardian.com/thumb.jpg"
        assert a["byline"] == "Jane Smith"
        assert a["source"] == "The Guardian"

    def test_strips_html_from_trail(self):
        raw = {
            "webTitle": "Test",
            "webUrl": "https://x",
            "fields": {"trailText": "<p>Hello <b>world</b></p>"},
        }
        a = normalize_article(raw)
        assert "<" not in a["summary"]
        assert "Hello world" in a["summary"]

    def test_truncates_long_summary(self):
        raw = {
            "webTitle": "T",
            "webUrl": "https://x",
            "fields": {"trailText": "x" * 200},
        }
        a = normalize_article(raw)
        assert len(a["summary"]) <= 143  # 140 + "…"

    def test_handles_missing_fields(self):
        raw = {"webTitle": "Minimal", "webUrl": "https://minimal"}
        a = normalize_article(raw)
        assert a["title"] == "Minimal"
        assert a["tag"] == "News"
        assert a["summary"] == ""
        assert a["thumb"] == ""

    def test_time_ago_recent(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _time_ago(now) == "just now"

    def test_time_ago_hours(self):
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        result = _time_ago(two_hours_ago)
        assert result.endswith("h ago")

    def test_time_ago_days(self):
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        result = _time_ago(three_days_ago)
        assert result.endswith("d ago")

    def test_time_ago_empty_string(self):
        assert _time_ago("") == ""


# ── prometheus_relevance_score ────────────────────────────────────────────────

class TestPrometheusRelevanceScore:

    def _article(self, title="", summary="", tag=""):
        return {
            "title": title,
            "summary": summary,
            "tag": tag,
            "published_iso": datetime.now(timezone.utc).isoformat(),
        }

    def test_ai_article_scores_high(self):
        a = self._article(title="OpenAI releases new AI agents framework")
        score = prometheus_relevance_score(a)
        assert score > 15, f"AI article should score >15, got {score}"

    def test_microschool_article_scores_high(self):
        a = self._article(title="Microschool movement grows in Florida", tag="Education")
        score = prometheus_relevance_score(a)
        assert score > 10

    def test_florida_article_gets_boost(self):
        a = self._article(title="South Florida hurricane season forecast")
        b = self._article(title="North Dakota weather outlook")
        assert prometheus_relevance_score(a) > prometheus_relevance_score(b)

    def test_celebrity_gossip_scores_lower(self):
        tech = self._article(title="New language model breaks benchmark records")
        gossip = self._article(title="Celebrity dating rumor confirmed")
        assert prometheus_relevance_score(tech) > prometheus_relevance_score(gossip)

    def test_recency_boost_for_fresh_articles(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        fresh = self._article(title="Market update")
        fresh["published_iso"] = now_iso
        stale = self._article(title="Market update")
        stale["published_iso"] = old_iso
        assert prometheus_relevance_score(fresh) > prometheus_relevance_score(stale)

    def test_tech_section_tag_gets_boost(self):
        a = self._article(title="Update", tag="technology")
        b = self._article(title="Update", tag="lifestyle")
        assert prometheus_relevance_score(a) >= prometheus_relevance_score(b)

    def test_analysis_title_gets_boost(self):
        a = self._article(title="Why AI agents are transforming software development")
        b = self._article(title="A thing happened yesterday")
        assert prometheus_relevance_score(a) > prometheus_relevance_score(b)

    def test_returns_float(self):
        a = self._article(title="test")
        assert isinstance(prometheus_relevance_score(a), float)


# ── pad_to_nine ───────────────────────────────────────────────────────────────

class TestPadToNine:

    def _article(self, i):
        return {
            "id": f"live-{i}",
            "title": f"Live article {i}",
            "href": f"https://guardian.com/{i}",
            "tag": "News",
            "summary": "",
            "thumb": "",
            "byline": "",
            "published_iso": "",
            "time_ago": "",
            "source": "The Guardian",
        }

    def test_pads_empty_list_to_nine(self):
        result = pad_to_nine([])
        assert len(result) == 9

    def test_pads_partial_list_to_nine(self):
        result = pad_to_nine([self._article(i) for i in range(3)])
        assert len(result) == 9

    def test_does_not_exceed_nine(self):
        result = pad_to_nine([self._article(i) for i in range(15)])
        assert len(result) == 9

    def test_live_articles_appear_first(self):
        live = [self._article(i) for i in range(4)]
        result = pad_to_nine(live)
        assert result[0]["id"] == "live-0"
        assert result[3]["id"] == "live-3"

    def test_no_duplicates_in_padded_result(self):
        partial = [self._article(i) for i in range(5)]
        result = pad_to_nine(partial)
        ids = [a["id"] for a in result]
        assert len(ids) == len(set(ids)), "pad_to_nine must not add duplicate ids"


# ── _fallback_articles ────────────────────────────────────────────────────────

class TestFallbackArticles:

    def test_returns_exactly_nine(self):
        articles = _fallback_articles()
        assert len(articles) == 9

    def test_all_have_required_fields(self):
        for a in _fallback_articles():
            assert a["title"], f"Missing title: {a}"
            assert a["id"], f"Missing id: {a}"

    def test_fallback_covers_prometheus_topics(self):
        titles = " ".join(a["title"].lower() for a in _fallback_articles())
        assert any(kw in titles for kw in ("ai", "agent", "automation", "calendar"))
        assert any(kw in titles for kw in ("education", "microschool", "school"))
        assert any(kw in titles for kw in ("florida", "miami", "south florida"))
        assert any(kw in titles for kw in ("market", "economy", "business"))

    def test_all_unique_ids(self):
        articles = _fallback_articles()
        ids = [a["id"] for a in articles]
        assert len(ids) == len(set(ids))


# ── get_news ──────────────────────────────────────────────────────────────────

class TestGetNews:

    def test_returns_demo_when_no_api_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_GUARDIAN_API_KEY", None)
            with patch("prometheus.services.guardian_news._load_env_key", return_value=("", "")):
                articles, status = get_news()
        assert status == "demo"
        assert len(articles) == 9

    def test_returns_live_on_successful_fetch(self):
        mock_raw = [
            {
                "id": f"test/{i}",
                "webTitle": f"AI story {i}",
                "webUrl": f"https://guardian.com/{i}",
                "sectionName": "Technology",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Story about AI.", "thumbnail": ""},
            }
            for i in range(12)
        ]

        with patch("prometheus.services.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.services.guardian_news._load_env_key", return_value=("test-key", "https://api")):
            articles, status = get_news(api_key="test-key")

        assert status == "live"
        assert 1 <= len(articles) <= 9

    def test_returns_fallback_on_network_error(self):
        from prometheus.services.guardian_news import fetch_guardian_articles as _fga
        with patch("prometheus.services.guardian_news.fetch_guardian_articles",
                   side_effect=RuntimeError("network error")), \
             patch("prometheus.services.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        assert status == "fallback"
        assert len(articles) == 9

    def test_always_returns_nine_articles(self):
        mock_raw = [
            {
                "id": f"art/{i}",
                "webTitle": f"Story {i}",
                "webUrl": f"https://guardian.com/{i}",
                "sectionName": "News",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {},
            }
            for i in range(3)  # only 3 raw results
        ]
        with patch("prometheus.services.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.services.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        assert len(articles) == 9


# ── fetch_guardian_articles (unit) ────────────────────────────────────────────

class TestFetchGuardianArticles:

    def test_raises_when_no_api_key(self):
        from prometheus.services.guardian_news import fetch_guardian_articles
        # Patch _load_env_key so it doesn't read the real .env file
        with patch("prometheus.services.guardian_news._load_env_key", return_value=("", "")):
            with pytest.raises(RuntimeError, match="VITE_GUARDIAN_API_KEY"):
                fetch_guardian_articles(api_key="", base_url="https://api")

    def test_builds_correct_query_params(self):
        from prometheus.services.guardian_news import fetch_guardian_articles
        import urllib.request

        captured_urls: list[str] = []

        class FakeResponse:
            def read(self):
                return json.dumps({"response": {"results": []}}).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return FakeResponse()

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            fetch_guardian_articles(api_key="my-test-key", base_url="https://api.guardian.com/search", page_size=50)

        assert captured_urls, "urlopen should have been called"
        url = captured_urls[0]
        assert "api-key=my-test-key" in url
        assert "show-fields=thumbnail" in url
        assert "order-by=newest" in url
        assert "page-size=50" in url

    def test_raises_on_http_error(self):
        from prometheus.services.guardian_news import fetch_guardian_articles
        import urllib.request, urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs=None, fp=None)

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            with pytest.raises(RuntimeError, match="403"):
                fetch_guardian_articles(api_key="key", base_url="https://api")

    def test_returns_empty_list_for_no_results(self):
        from prometheus.services.guardian_news import fetch_guardian_articles
        import urllib.request

        class FakeResponse:
            def read(self):
                return json.dumps({"response": {"results": []}}).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        with patch.object(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse()):
            results = fetch_guardian_articles(api_key="key", base_url="https://api")
        assert results == []


# ── HUD import fixes confirmed ────────────────────────────────────────────────

class TestHUDImports:

    def test_store_importable(self):
        from jarvis_desktop_hud import Store
        store = Store()
        assert hasattr(store, "mission")
        assert hasattr(store, "chat_history")
        assert hasattr(store, "activity_filter")
        assert hasattr(store, "diagnostic")
        assert hasattr(store, "cost_log")

    def test_system_stats_importable(self):
        from jarvis_desktop_hud import SystemStats
        stats = SystemStats()
        assert hasattr(stats, "cpu")
        assert hasattr(stats, "ram")
        assert hasattr(stats, "disk")

    def test_hud_window_importable(self):
        from jarvis_desktop_hud import HUDWindow
        assert hasattr(HUDWindow, "_draw_mission_strip")

    def test_mission_file_constant(self):
        import jarvis_desktop_hud
        assert hasattr(jarvis_desktop_hud, "MISSION_FILE")
        assert "mission_state.json" in str(jarvis_desktop_hud.MISSION_FILE)

    def test_news_card_importable(self):
        from jarvis_desktop_hud import NewsCard
        assert NewsCard is not None
