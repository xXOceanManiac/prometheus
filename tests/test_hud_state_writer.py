"""
tests/test_hud_state_writer.py

Tests for hud_state_writer: canonical path, news in state, calendar in state,
fallback, file write, schema.
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


# ── Canonical path ────────────────────────────────────────────────────────────

class TestCanonicalPath:

    def test_canonical_path_is_under_desktop_prometheus_state(self):
        from prometheus.services.hud_state_writer import _DASHBOARD_STATE_PATH
        assert "Desktop" in str(_DASHBOARD_STATE_PATH)
        assert "PROMETHEUS" in str(_DASHBOARD_STATE_PATH)
        assert "state" in str(_DASHBOARD_STATE_PATH)
        assert "dashboard_state.json" in str(_DASHBOARD_STATE_PATH)

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
