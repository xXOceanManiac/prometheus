"""
tests/test_guardian_news.py

Unit tests for prometheus.news.guardian_news.

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

from prometheus.news.guardian_news import (
    normalize_article,
    prometheus_relevance_score,
    pad_to_ten,
    get_news,
    _fallback_articles,
    _time_ago,
    _VIOLENT_TITLE_TERMS,
    _is_disallowed_hud_headline,
    _HARD_EXCLUSION_TITLE_PATTERNS,
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

    def test_thumbnail_and_thumb_both_present_when_api_provides_thumbnail(self):
        raw = {
            "webTitle": "AI story",
            "webUrl": "https://theguardian.com/tech",
            "fields": {"thumbnail": "https://media.guim.co.uk/thumb.jpg"},
        }
        a = normalize_article(raw)
        assert a["thumb"] == "https://media.guim.co.uk/thumb.jpg"
        assert a["thumbnail"] == "https://media.guim.co.uk/thumb.jpg"
        assert a["thumb"] == a["thumbnail"], "thumb and thumbnail must be equal"

    def test_thumbnail_and_thumb_empty_when_no_api_thumbnail(self):
        raw = {"webTitle": "Story", "webUrl": "https://x", "fields": {}}
        a = normalize_article(raw)
        assert a["thumb"] == ""
        assert a["thumbnail"] == ""

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

    def test_entrepreneurship_scores_high(self):
        a = self._article(title="Founder builds marketplace for indie developers", tag="Business")
        score = prometheus_relevance_score(a)
        assert score > 10, f"Entrepreneurship article should score >10, got {score}"

    def test_science_space_scores_high(self):
        a = self._article(title="NASA breakthrough in quantum computing engineering")
        score = prometheus_relevance_score(a)
        assert score > 15

    def test_education_microschool_scores_high(self):
        a = self._article(title="School choice legislation expands microschool options in Arizona")
        score = prometheus_relevance_score(a)
        assert score > 10

    def test_arizona_utah_gets_geographic_boost(self):
        a = self._article(title="Arizona startup ecosystem grows amid school choice boom")
        b = self._article(title="Generic startup news")
        assert prometheus_relevance_score(a) > prometheus_relevance_score(b)


# ── violent/sensational title penalties ───────────────────────────────────────

class TestViolentTitlePenalty:

    def _article(self, title="", summary="", tag="News"):
        return {
            "title": title,
            "summary": summary,
            "tag": tag,
            "published_iso": datetime.now(timezone.utc).isoformat(),
        }

    def test_violent_title_scores_lower_than_neutral(self):
        violent = self._article(title="Man killed in downtown stabbing attack")
        neutral = self._article(title="New software platform launches for developers")
        assert prometheus_relevance_score(violent) < prometheus_relevance_score(neutral)

    def test_murder_headline_heavily_penalized(self):
        a = self._article(title="Three murdered in overnight attack")
        score = prometheus_relevance_score(a)
        assert score < 0, f"Murder headline should score negative, got {score}"

    def test_bombing_headline_heavily_penalized(self):
        a = self._article(title="Bombing kills dozens in city center")
        score = prometheus_relevance_score(a)
        assert score < 0

    def test_drone_strike_penalized(self):
        a = self._article(title="Drone strike kills civilians in conflict zone")
        score = prometheus_relevance_score(a)
        assert score < 0

    def test_horror_title_penalized(self):
        a = self._article(title="Horrifying discovery shocks investigators")
        score = prometheus_relevance_score(a)
        assert score < 5

    def test_analytical_geopolitics_can_survive(self):
        # An analytical article about geopolitics without violent headline terms should score OK
        a = self._article(
            title="Why the US-China semiconductor rivalry is reshaping global trade",
            summary="Analysis of technology competition and supply chain realignment.",
            tag="World",
        )
        score = prometheus_relevance_score(a)
        assert score > 0, f"Analytical geopolitics should not be eliminated, got {score}"

    def test_violent_title_terms_list_is_nonempty(self):
        assert len(_VIOLENT_TITLE_TERMS) > 10

    def test_all_violent_terms_are_lowercase(self):
        for term in _VIOLENT_TITLE_TERMS:
            assert term == term.lower(), f"Violent term must be lowercase: {term!r}"


# ── _is_disallowed_hud_headline (hard exclusion) ─────────────────────────────

class TestHardExclusion:
    """Tests for _is_disallowed_hud_headline hard-exclusion filter."""

    def test_obituary_in_title_excluded(self):
        assert _is_disallowed_hud_headline(
            "Richard Scolyer, acclaimed researcher — obituary", "World", ""
        ) is True

    def test_obituary_section_excluded(self):
        assert _is_disallowed_hud_headline(
            "Ruth Artmonsky", "Obituaries", ""
        ) is True

    def test_obituaries_section_case_insensitive(self):
        assert _is_disallowed_hud_headline(
            "Someone Notable", "obituaries", ""
        ) is True

    def test_dies_aged_excluded(self):
        assert _is_disallowed_hud_headline(
            "Cancer researcher dies aged 59", "World", ""
        ) is True

    def test_died_aged_excluded(self):
        assert _is_disallowed_hud_headline(
            "Former president died aged 91", "World", ""
        ) is True

    def test_has_died_excluded(self):
        assert _is_disallowed_hud_headline(
            "Nobel laureate has died", "Science", ""
        ) is True

    def test_drone_hits_nuclear_fuel_excluded(self):
        assert _is_disallowed_hud_headline(
            "Russian drone hits building storing spent nuclear fuel near Chornobyl",
            "World", ""
        ) is True

    def test_drone_hits_excluded(self):
        assert _is_disallowed_hud_headline(
            "Drone hits hospital in conflict zone", "World", ""
        ) is True

    def test_nuclear_disaster_excluded(self):
        assert _is_disallowed_hud_headline(
            "Nuclear disaster risk grows at stricken plant", "World", ""
        ) is True

    def test_ai_article_not_excluded(self):
        assert _is_disallowed_hud_headline(
            "OpenAI releases new agent framework for developers", "Technology", ""
        ) is False

    def test_microschool_article_not_excluded(self):
        assert _is_disallowed_hud_headline(
            "Microschool movement grows in Florida", "Education", ""
        ) is False

    def test_nuclear_energy_policy_not_excluded(self):
        assert _is_disallowed_hud_headline(
            "Why nuclear energy is back on the table for US grid resilience", "Energy", ""
        ) is False

    def test_startup_article_not_excluded(self):
        assert _is_disallowed_hud_headline(
            "Founder builds marketplace for indie developers", "Business", ""
        ) is False

    def test_hard_exclusion_patterns_nonempty(self):
        assert len(_HARD_EXCLUSION_TITLE_PATTERNS) >= 5

    def test_obituary_excluded_from_get_news(self):
        """get_news must not return obituary headlines even if they appear in the live feed."""
        obit_raw = [
            {
                "id": f"obit/{i}",
                "webTitle": f"Famous person — obituary" if i == 0 else f"Someone, dies aged {50 + i}",
                "webUrl": f"https://guardian.com/obit/{i}",
                "sectionName": "World",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Obituary text.", "thumbnail": ""},
            }
            for i in range(5)
        ]
        calm_raw = [
            {
                "id": f"calm/{i}",
                "webTitle": f"AI developer tools advance rapidly {i}",
                "webUrl": f"https://guardian.com/calm/{i}",
                "sectionName": "Technology",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Progress in AI tooling.", "thumbnail": ""},
            }
            for i in range(15)
        ]
        mock_raw = obit_raw + calm_raw
        with patch("prometheus.news.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        titles = [a["title"].lower() for a in articles]
        for title in titles:
            assert "obituary" not in title, f"Obituary must not appear: {title!r}"
            assert "dies aged" not in title, f"Death notice must not appear: {title!r}"

    def test_drone_nuclear_excluded_from_get_news(self):
        """Drone hits nuclear fuel framing must not appear in results."""
        bad_raw = [
            {
                "id": "drone/1",
                "webTitle": "Russian drone hits building storing spent nuclear fuel near Chornobyl",
                "webUrl": "https://guardian.com/drone/1",
                "sectionName": "World",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Incident near Chornobyl.", "thumbnail": ""},
            }
        ]
        calm_raw = [
            {
                "id": f"calm/{i}",
                "webTitle": f"Space technology and engineering news {i}",
                "webUrl": f"https://guardian.com/calm/{i}",
                "sectionName": "Science",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Space news.", "thumbnail": ""},
            }
            for i in range(15)
        ]
        mock_raw = bad_raw + calm_raw
        with patch("prometheus.news.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        titles = [a["title"].lower() for a in articles]
        for title in titles:
            assert "drone hits" not in title, f"Drone-hits title must not appear: {title!r}"
            assert "nuclear fuel" not in title, f"Nuclear fuel disaster must not appear: {title!r}"


# ── pad_to_ten ────────────────────────────────────────────────────────────────

class TestPadToTen:

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

    def test_pads_empty_list_to_ten(self):
        result = pad_to_ten([])
        assert len(result) == 10

    def test_pads_partial_list_to_ten(self):
        result = pad_to_ten([self._article(i) for i in range(3)])
        assert len(result) == 10

    def test_does_not_exceed_ten(self):
        result = pad_to_ten([self._article(i) for i in range(15)])
        assert len(result) == 10

    def test_live_articles_appear_first(self):
        live = [self._article(i) for i in range(4)]
        result = pad_to_ten(live)
        assert result[0]["id"] == "live-0"
        assert result[3]["id"] == "live-3"

    def test_no_duplicates_in_padded_result(self):
        partial = [self._article(i) for i in range(5)]
        result = pad_to_ten(partial)
        ids = [a["id"] for a in result]
        assert len(ids) == len(set(ids)), "pad_to_ten must not add duplicate ids"

    def test_ten_articles_cycle_cleanly_in_pairs(self):
        # 10 articles / 2 per set = 5 clean pairs, no leftover single-article slide
        result = pad_to_ten([self._article(i) for i in range(10)])
        assert len(result) == 10
        assert len(result) % 2 == 0, "10 articles should divide evenly into pairs"


# ── _fallback_articles ────────────────────────────────────────────────────────

class TestFallbackArticles:

    def test_returns_exactly_ten(self):
        articles = _fallback_articles()
        assert len(articles) == 10

    def test_all_have_required_fields(self):
        for a in _fallback_articles():
            assert a["title"], f"Missing title: {a}"
            assert a["id"], f"Missing id: {a}"

    def test_fallback_articles_have_both_thumb_and_thumbnail(self):
        for a in _fallback_articles():
            assert "thumb" in a, f"Missing thumb key: {a['id']}"
            assert "thumbnail" in a, f"Missing thumbnail key: {a['id']}"

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
            with patch("prometheus.news.guardian_news._load_env_key", return_value=("", "")):
                articles, status = get_news()
        assert status == "demo"
        assert len(articles) == 10

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

        with patch("prometheus.news.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("test-key", "https://api")):
            articles, status = get_news(api_key="test-key")

        assert status == "live"
        assert 1 <= len(articles) <= 10

    def test_returns_fallback_on_network_error(self):
        with patch("prometheus.news.guardian_news.fetch_guardian_articles",
                   side_effect=RuntimeError("network error")), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        assert status == "fallback"
        assert len(articles) == 10

    def test_always_returns_ten_articles(self):
        mock_raw = [
            {
                "id": f"art/{i}",
                "webTitle": f"Story {i}",
                "webUrl": f"https://guardian.com/{i}",
                "sectionName": "News",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {},
            }
            for i in range(3)  # only 3 raw results — should pad to 10
        ]
        with patch("prometheus.news.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        assert len(articles) == 10

    def test_violent_articles_excluded_from_top_ten(self):
        """Violent headlines should score too low to appear in the top-10 live results."""
        violent_raw = [
            {
                "id": f"violent/{i}",
                "webTitle": f"Man killed in brutal shooting attack {i}",
                "webUrl": f"https://guardian.com/violent/{i}",
                "sectionName": "UK News",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Shooting incident reported.", "thumbnail": ""},
            }
            for i in range(8)
        ]
        calm_raw = [
            {
                "id": f"calm/{i}",
                "webTitle": f"AI and software developer tools advance {i}",
                "webUrl": f"https://guardian.com/calm/{i}",
                "sectionName": "Technology",
                "webPublicationDate": datetime.now(timezone.utc).isoformat(),
                "fields": {"trailText": "Progress in developer tooling.", "thumbnail": ""},
            }
            for i in range(12)
        ]
        mock_raw = violent_raw + calm_raw
        with patch("prometheus.news.guardian_news.fetch_guardian_articles", return_value=mock_raw), \
             patch("prometheus.news.guardian_news._load_env_key", return_value=("key", "url")):
            articles, status = get_news(api_key="key")
        titles = [a["title"].lower() for a in articles]
        # No violent headline should be in the result set
        for title in titles:
            assert "killed" not in title and "shooting" not in title, \
                f"Violent article should not appear in results: {title!r}"


# ── fetch_guardian_articles (unit) ────────────────────────────────────────────

class TestFetchGuardianArticles:

    def test_raises_when_no_api_key(self):
        from prometheus.news.guardian_news import fetch_guardian_articles
        # Patch _load_env_key so it doesn't read the real .env file
        with patch("prometheus.news.guardian_news._load_env_key", return_value=("", "")):
            with pytest.raises(RuntimeError, match="VITE_GUARDIAN_API_KEY"):
                fetch_guardian_articles(api_key="", base_url="https://api")

    def test_builds_correct_query_params(self):
        from prometheus.news.guardian_news import fetch_guardian_articles
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
        from prometheus.news.guardian_news import fetch_guardian_articles
        import urllib.request, urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs=None, fp=None)

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            with pytest.raises(RuntimeError, match="403"):
                fetch_guardian_articles(api_key="key", base_url="https://api")

    def test_returns_empty_list_for_no_results(self):
        from prometheus.news.guardian_news import fetch_guardian_articles
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


