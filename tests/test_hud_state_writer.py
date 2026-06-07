"""
tests/test_hud_state_writer.py

Tests for:
- hud_state_writer: canonical path, news in state, calendar in state, fallback, file write, schema
- readonly_dashboard: /health, /state, /news, HTML page, POST rejected, no secrets,
                      calendar rail, analog clock
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

def _mock_articles(n: int = 10) -> list[dict]:
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


def _mock_cal_events(n: int = 3) -> list[dict]:
    return [
        {
            "event_id": f"evt-{i}",
            "title": f"Meeting {i}",
            "start_time": f"2026-06-06T{9 + i:02d}:00:00-04:00",
            "end_time": f"2026-06-06T{10 + i:02d}:00:00-04:00",
            "location": "",
            "description": "",
            "calendar_id": "primary",
            "is_all_day": False,
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

    def test_news_card_has_10_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(10), "live")
        assert len(state["cards"]["news"]["articles"]) == 10

    def test_news_chip_live(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert build_hud_state(_mock_articles(), "live")["cards"]["news"]["chip"] == "LIVE"

    def test_news_chip_demo(self):
        from prometheus.services.hud_state_writer import build_hud_state
        assert build_hud_state(_mock_articles(), "demo")["cards"]["news"]["chip"] == "DEMO"

    def test_items_populated_from_first_3_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        items = build_hud_state(_mock_articles(10), "live")["cards"]["news"]["items"]
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


# ── Calendar card ─────────────────────────────────────────────────────────────

class TestCalendarCard:

    def test_calendar_card_present_in_build_hud_state(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(3), "live", "2026-06-06")
        assert "calendar" in state["cards"], "cards.calendar must be present"

    def test_calendar_focus_card_mirrors_calendar(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(3), "live", "2026-06-06")
        # Godot maps "focus" → zone_calendar; both must be populated
        assert "focus" in state["cards"]
        assert state["cards"]["focus"]["status"] == "live"

    def test_calendar_live_has_events_list(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(3), "live", "2026-06-06")
        cal = state["cards"]["calendar"]
        assert cal["status"] == "live"
        assert isinstance(cal["events"], list)
        assert len(cal["events"]) == 3

    def test_calendar_events_have_required_fields(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(2), "live", "2026-06-06")
        for ev in state["cards"]["calendar"]["events"]:
            assert "title" in ev
            assert "start_time" in ev
            assert "time_label" in ev
            assert "is_now" in ev
            assert "is_next" in ev

    def test_calendar_items_is_list_of_strings(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(3), "live", "2026-06-06")
        items = state["cards"]["calendar"]["items"]
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)

    def test_calendar_pending_when_no_events(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", [], "pending", "2026-06-06")
        cal = state["cards"]["calendar"]
        assert cal["status"] == "pending"
        assert cal["events"] == []

    def test_calendar_error_state(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", [], "error", "2026-06-06")
        assert state["cards"]["calendar"]["status"] == "error"

    def test_calendar_chip_live(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(1), "live", "2026-06-06")
        assert state["cards"]["calendar"]["chip"] == "LIVE"

    def test_calendar_chip_pending(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", [], "pending", "2026-06-06")
        assert state["cards"]["calendar"]["chip"] == "PENDING"

    def test_calendar_date_preserved(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(2), "live", "2026-06-06")
        assert state["cards"]["calendar"]["date"] == "2026-06-06"

    def test_calendar_summary_contains_count(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(), "live", _mock_cal_events(3), "live", "2026-06-06")
        summary = state["cards"]["calendar"]["summary"]
        assert "3" in summary

    def test_build_hud_state_backward_compat_no_cal_args(self):
        # build_hud_state(articles, status) still works (cal defaults to pending)
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(9), "live")
        assert "calendar" in state["cards"]
        assert state["cards"]["calendar"]["status"] == "pending"

    def test_today_now_can_consume_next_event(self):
        from prometheus.services.hud_state_writer import build_hud_state
        evs = _mock_cal_events(2)
        state = build_hud_state(_mock_articles(), "live", evs, "live", "2026-06-06")
        # calendar and focus cards must exist for today_now to consume
        assert "calendar" in state["cards"]
        cal_evs = state["cards"]["calendar"]["events"]
        assert len(cal_evs) == 2


# ── Calendar card payload builder ─────────────────────────────────────────────

class TestCalendarCardPayload:

    def test_is_next_flagged_for_future_event(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        # Event far in the future
        evs = [
            {
                "title": "Future Meeting",
                "start_time": "2099-01-01T10:00:00+00:00",
                "end_time": "2099-01-01T11:00:00+00:00",
                "is_all_day": False,
            }
        ]
        payload = _calendar_card_payload(evs, "2099-01-01", "live")
        assert payload["events"][0]["is_next"] is True

    def test_pending_payload_has_empty_events(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        payload = _calendar_card_payload([], "2026-06-06", "pending")
        assert payload["status"] == "pending"
        assert payload["events"] == []
        assert payload["items"] == []

    def test_all_day_event_gets_all_day_label(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        evs = [{"title": "Holiday", "start_time": "2026-06-06", "end_time": "2026-06-07", "is_all_day": True}]
        payload = _calendar_card_payload(evs, "2026-06-06", "live")
        assert payload["events"][0]["time_label"] == "All Day"


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
            _mod.write_dashboard_state(_mock_articles(10), "live")
            data = json.loads(_mod._DASHBOARD_STATE_PATH.read_text())
            assert len(data["cards"]["news"]["articles"]) == 10
        finally:
            _mod._DASHBOARD_STATE_PATH = original

    def test_calendar_in_written_file(self, tmp_path):
        import prometheus.services.hud_state_writer as _mod
        original = _mod._DASHBOARD_STATE_PATH
        _mod._DASHBOARD_STATE_PATH = tmp_path / "dashboard_state.json"
        try:
            _mod.write_dashboard_state(_mock_articles(9), "live", _mock_cal_events(2), "live", "2026-06-06")
            data = json.loads(_mod._DASHBOARD_STATE_PATH.read_text())
            assert "calendar" in data["cards"]
            assert data["cards"]["calendar"]["status"] == "live"
            assert len(data["cards"]["calendar"]["events"]) == 2
        finally:
            _mod._DASHBOARD_STATE_PATH = original


# ── Guardian news fallback ────────────────────────────────────────────────────

class TestFetchNewsInternal:

    def test_fallback_when_no_api_key(self):
        from prometheus.services.guardian_news import get_news
        with patch("prometheus.services.guardian_news._load_env_key", return_value=("", "")):
            articles, status = get_news()
        assert status == "demo"
        assert len(articles) == 10

    def test_fetch_news_returns_fallback_on_network_error(self):
        from prometheus.services.hud_state_writer import _fetch_news
        with patch("prometheus.services.guardian_news.fetch_guardian_articles",
                   side_effect=RuntimeError("network error")), \
             patch("prometheus.services.guardian_news._load_env_key",
                   return_value=("some-key", "https://api")):
            articles, status = _fetch_news()
        assert status == "fallback"
        assert len(articles) == 10


# ── ReadonlyDashboard ─────────────────────────────────────────────────────────

def _make_state_with_calendar(tmp_path: Path, cal_events: list | None = None, n_articles: int = 10) -> Path:
    """Write a full dashboard_state.json to tmp_path and return the path."""
    state_file = tmp_path / "dashboard_state.json"
    state_file.write_text(json.dumps({
        "state": "idle",
        "active_project": "TestProject",
        "updated_at": "2026-06-06T10:00:00Z",
        "cards": {
            "news": {
                "title": "News",
                "chip": "LIVE",
                "status": "live",
                "articles": _mock_articles(n_articles),
                "items": [],
            },
            "activity": {"title": "Activity", "chip": "LIVE", "items": ["event_a"], "summary": ""},
            "calendar": {
                "title": "Today",
                "chip": "LIVE" if cal_events else "PENDING",
                "status": "live" if cal_events else "pending",
                "date": "2026-06-06",
                "summary": f"{len(cal_events or [])} events today",
                "events": cal_events or [],
                "items": [f"9:0{i} AM Meeting {i}" for i in range(len(cal_events or []))],
            },
        },
    }), encoding="utf-8")
    return state_file


class TestReadonlyDashboard:

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path):
        from prometheus.services.readonly_dashboard import ReadonlyDashboard
        import prometheus.services.readonly_dashboard as _mod

        port = _find_free_port()
        original = _mod._DASHBOARD_STATE_PATH
        state_file = _make_state_with_calendar(tmp_path, _mock_cal_events(2))
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

    def test_state_exposes_calendar_card(self):
        code, body = self._get("/state")
        assert code == 200
        d = json.loads(body)
        assert "calendar" in d["cards"], "cards.calendar must be present in /state"
        assert d["cards"]["calendar"]["status"] == "live"

    def test_news_has_10_articles(self):
        code, body = self._get("/news")
        assert code == 200
        assert len(json.loads(body)["articles"]) == 10

    def test_root_html_is_html(self):
        code, body = self._get("/")
        assert code == 200
        assert b"<html" in body.lower()

    def test_root_html_includes_news_title(self):
        code, body = self._get("/")
        assert code == 200
        assert b"Article 0" in body

    def test_root_html_shows_project(self):
        _, body = self._get("/")
        assert b"TestProject" in body

    def test_root_html_has_calendar_rail(self):
        _, body = self._get("/")
        html = body.decode()
        assert "cal-rail" in html, "Right-side calendar rail must be present"

    def test_root_html_calendar_events_rendered(self):
        _, body = self._get("/")
        html = body.decode()
        # Events from _mock_cal_events(2) should appear
        assert "Meeting 0" in html, "Calendar event title must appear in HTML"
        assert "Meeting 1" in html

    def test_root_html_analog_clock_present(self):
        _, body = self._get("/")
        html = body.decode()
        assert "clock-face" in html, "Analog clock SVG must be present"
        assert "hand-h" in html, "Hour hand must be present"
        assert "hand-m" in html, "Minute hand must be present"
        assert "hand-s" in html, "Second hand must be present"

    def test_root_html_calendar_pending_state(self):
        """When calendar has no events (pending), show a clear pending state."""
        import prometheus.services.readonly_dashboard as _mod
        original = _mod._DASHBOARD_STATE_PATH
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".json"))
        tmp.write_text(json.dumps({
            "state": "idle", "active_project": "P",
            "updated_at": "2026-06-06T00:00:00Z",
            "cards": {
                "news": {"title": "N", "chip": "LIVE", "status": "live", "articles": [], "items": []},
                "calendar": {"title": "Today", "chip": "PENDING", "status": "pending",
                             "date": "2026-06-06", "summary": "pending", "events": [], "items": []},
            },
        }), encoding="utf-8")
        _mod._DASHBOARD_STATE_PATH = tmp
        try:
            code, body = self._get("/")
            assert code == 200
            html = body.decode()
            assert "cal-pending" in html or "pending" in html.lower()
        finally:
            _mod._DASHBOARD_STATE_PATH = original
            tmp.unlink(missing_ok=True)

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

    def test_no_secrets_in_html(self):
        _, body = self._get("/")
        text = body.decode().lower()
        assert "sk-" not in text

    def test_api_key_values_redacted(self):
        import prometheus.services.readonly_dashboard as _mod
        original = _mod._DASHBOARD_STATE_PATH

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"state": "idle", "vite_guardian_api_key": "secret-key-123"}, f)
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
        articles = build_hud_state(_mock_articles(10), "live")["cards"]["news"]["articles"]
        assert isinstance(articles, list)
        assert len(articles) == 10

    def test_each_article_has_title_and_section(self):
        from prometheus.services.hud_state_writer import build_hud_state
        for a in build_hud_state(_mock_articles(10), "live")["cards"]["news"]["articles"]:
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

    def test_news_still_has_exactly_10_articles(self):
        from prometheus.services.hud_state_writer import build_hud_state
        state = build_hud_state(_mock_articles(10), "live", _mock_cal_events(3), "live", "2026-06-06")
        assert len(state["cards"]["news"]["articles"]) == 10


# ── Thumbnail end-to-end ──────────────────────────────────────────────────────

def _mock_articles_with_thumb(n: int = 10) -> list[dict]:
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
                    "articles": _mock_articles_with_thumb(10),
                    "items": [],
                },
                "calendar": {
                    "title": "Today", "chip": "LIVE", "status": "live",
                    "date": "2026-06-06", "summary": "1 event today",
                    "events": [{"title": "Thumb Test Event", "start_time": "2026-06-06T09:00:00-04:00",
                                 "end_time": "2026-06-06T10:00:00-04:00", "time_label": "9:00 AM",
                                 "location": "", "source": "Google Calendar",
                                 "is_now": False, "is_next": True}],
                    "items": ["9:00 AM Thumb Test Event"],
                },
            },
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

    def test_news_endpoint_exposes_thumb_field(self):
        code, body = self._get("/news")
        assert code == 200
        data = json.loads(body)
        articles = data.get("articles", [])
        assert len(articles) == 10
        assert articles[0].get("thumb") == "https://media.guim.co.uk/sample/500.jpg"

    def test_html_includes_img_tag_when_thumb_present(self):
        _, body = self._get("/")
        assert b'<img' in body
        assert b'media.guim.co.uk/sample/500.jpg' in body

    def test_html_no_broken_img_when_thumb_missing(self):
        _, body = self._get("/")
        html = body.decode()
        import re
        empty_src_imgs = re.findall(r'<img[^>]+src=""', html)
        assert empty_src_imgs == [], f"Found empty-src img tags: {empty_src_imgs}"

    def test_html_calendar_event_in_rail(self):
        _, body = self._get("/")
        assert b"Thumb Test Event" in body, "Calendar event must appear in rail"

    def test_no_secrets_in_html(self):
        _, body = self._get("/")
        text = body.decode().lower()
        assert "sk-" not in text
        assert "api_key" not in text or "[redacted]" in text or "read-only" in text


# ── GOAL E: Calendar refresh default 60s ─────────────────────────────────────

class TestCalendarRefreshDefault:

    def test_cal_refresh_default_is_60s(self):
        import importlib
        import prometheus.services.hud_state_writer as _mod
        saved = os.environ.pop("PROMETHEUS_CAL_REFRESH_SECONDS", None)
        try:
            importlib.reload(_mod)
            assert _mod._CAL_REFRESH_SECONDS == 60
        finally:
            if saved is not None:
                os.environ["PROMETHEUS_CAL_REFRESH_SECONDS"] = saved
            importlib.reload(_mod)

    def test_cal_refresh_env_override(self):
        import importlib
        import prometheus.services.hud_state_writer as _mod
        saved = os.environ.get("PROMETHEUS_CAL_REFRESH_SECONDS")
        os.environ["PROMETHEUS_CAL_REFRESH_SECONDS"] = "300"
        try:
            importlib.reload(_mod)
            assert _mod._CAL_REFRESH_SECONDS == 300
        finally:
            if saved is not None:
                os.environ["PROMETHEUS_CAL_REFRESH_SECONDS"] = saved
            else:
                os.environ.pop("PROMETHEUS_CAL_REFRESH_SECONDS", None)
            importlib.reload(_mod)


# ── GOAL F: Google Calendar event color fields ─────────────────────────────────

class TestGoogleColorMap:

    def test_color_map_has_11_entries(self):
        from prometheus.services.hud_state_writer import _GOOGLE_COLOR_MAP
        assert len(_GOOGLE_COLOR_MAP) == 11

    def test_color_map_keys_are_strings_1_to_11(self):
        from prometheus.services.hud_state_writer import _GOOGLE_COLOR_MAP
        assert set(_GOOGLE_COLOR_MAP.keys()) == {str(i) for i in range(1, 12)}

    def test_color_map_values_are_hex_strings(self):
        from prometheus.services.hud_state_writer import _GOOGLE_COLOR_MAP
        for k, v in _GOOGLE_COLOR_MAP.items():
            assert v.startswith("#") and len(v) == 7, f"Bad hex for colorId {k!r}: {v!r}"

    def test_peacock_is_correct_hex(self):
        from prometheus.services.hud_state_writer import _GOOGLE_COLOR_MAP
        assert _GOOGLE_COLOR_MAP["7"] == "#039BE5"  # Peacock


class TestCalendarEventColorFields:

    def _events_with_color(self, color_id: str = "7") -> list[dict]:
        return [
            {
                "title": "Color Meeting",
                "start_time": "2099-01-01T10:00:00+00:00",
                "end_time": "2099-01-01T11:00:00+00:00",
                "is_all_day": False,
                "color_id": color_id,
            }
        ]

    def test_event_with_color_id_gets_color_hex(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        payload = _calendar_card_payload(self._events_with_color("7"), "2099-01-01", "live")
        ev = payload["events"][0]
        assert ev["color_id"] == "7"
        assert ev["color_hex"] == "#039BE5"
        assert ev["accent_color"] == "#039BE5"

    def test_event_without_color_id_gets_empty_strings(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        evs = [
            {
                "title": "No Color",
                "start_time": "2099-01-01T10:00:00+00:00",
                "end_time": "2099-01-01T11:00:00+00:00",
                "is_all_day": False,
            }
        ]
        payload = _calendar_card_payload(evs, "2099-01-01", "live")
        ev = payload["events"][0]
        assert ev["color_id"] == ""
        assert ev["color_hex"] == ""
        assert ev["accent_color"] == ""

    def test_event_with_unknown_color_id_gets_empty_hex(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload
        payload = _calendar_card_payload(self._events_with_color("99"), "2099-01-01", "live")
        ev = payload["events"][0]
        assert ev["color_id"] == "99"
        assert ev["color_hex"] == ""
        assert ev["accent_color"] == ""

    def test_all_11_color_ids_resolve(self):
        from prometheus.services.hud_state_writer import _calendar_card_payload, _GOOGLE_COLOR_MAP
        for cid in [str(i) for i in range(1, 12)]:
            evs = [
                {
                    "title": f"Event {cid}",
                    "start_time": "2099-01-01T10:00:00+00:00",
                    "end_time": "2099-01-01T11:00:00+00:00",
                    "is_all_day": False,
                    "color_id": cid,
                }
            ]
            payload = _calendar_card_payload(evs, "2099-01-01", "live")
            ev = payload["events"][0]
            assert ev["color_hex"] == _GOOGLE_COLOR_MAP[cid], f"Wrong hex for colorId={cid}"
            assert ev["accent_color"] == _GOOGLE_COLOR_MAP[cid]

    def test_required_color_fields_always_present(self):
        from prometheus.services.hud_state_writer import build_hud_state
        evs = _mock_cal_events(3)
        state = build_hud_state(_mock_articles(), "live", evs, "live", "2026-06-06")
        for ev in state["cards"]["calendar"]["events"]:
            assert "color_id" in ev, "color_id must be present"
            assert "color_hex" in ev, "color_hex must be present"
            assert "accent_color" in ev, "accent_color must be present"


class TestEventToDictColorId:

    def test_color_id_extracted_from_raw(self):
        from prometheus.integrations.google_calendar import GoogleCalendarEvent
        from prometheus.agents.calendar_read_tools import _event_to_dict
        raw = {"colorId": "3", "summary": "Test Event"}
        ev = GoogleCalendarEvent(
            event_id="e1",
            calendar_id="primary",
            title="Test Event",
            start_time="2026-06-06T09:00:00Z",
            end_time="2026-06-06T10:00:00Z",
            location=None,
            description=None,
            html_link=None,
            raw=raw,
        )
        d = _event_to_dict(ev)
        assert d["color_id"] == "3"

    def test_color_id_empty_when_raw_missing(self):
        from prometheus.integrations.google_calendar import GoogleCalendarEvent
        from prometheus.agents.calendar_read_tools import _event_to_dict
        ev = GoogleCalendarEvent(
            event_id="e2",
            calendar_id="primary",
            title="No Color",
            start_time="2026-06-06T09:00:00Z",
            end_time=None,
            location=None,
            description=None,
            html_link=None,
            raw=None,
        )
        d = _event_to_dict(ev)
        assert d["color_id"] == ""

    def test_color_id_empty_when_colorId_absent_in_raw(self):
        from prometheus.integrations.google_calendar import GoogleCalendarEvent
        from prometheus.agents.calendar_read_tools import _event_to_dict
        ev = GoogleCalendarEvent(
            event_id="e3",
            calendar_id="primary",
            title="No Color Field",
            start_time="2026-06-06T09:00:00Z",
            end_time=None,
            location=None,
            description=None,
            html_link=None,
            raw={"summary": "No colorId"},
        )
        d = _event_to_dict(ev)
        assert d["color_id"] == ""
