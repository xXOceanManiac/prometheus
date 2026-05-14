"""
test_log_viewer.py — Unit tests for prometheus/infra/log_viewer.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── Module smoke ──────────────────────────────────────────────────────────────

class TestModuleImport:
    def test_imports_cleanly(self):
        from prometheus.infra import log_viewer  # noqa: F401

    def test_functions_exist(self):
        from prometheus.infra.log_viewer import (
            list_log_files,
            read_latest_log_tail,
            read_log_tail,
        )
        assert callable(list_log_files)
        assert callable(read_latest_log_tail)
        assert callable(read_log_tail)


# ── list_log_files ────────────────────────────────────────────────────────────

class TestListLogFiles:
    def test_returns_list_when_dir_missing(self, tmp_path):
        fake_dir = tmp_path / "nonexistent"
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", fake_dir):
            from prometheus.infra import log_viewer
            result = log_viewer.list_log_files()
        assert result == []

    def test_returns_metadata_for_jsonl_files(self, tmp_path):
        (tmp_path / "2026-05-14.jsonl").write_text('{"ts":"2026-05-14 10:00:00","kind":"test"}\n')
        (tmp_path / "2026-05-13.jsonl").write_text('{"ts":"2026-05-13 10:00:00","kind":"test"}\n')
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            files = log_viewer.list_log_files()
        assert len(files) == 2
        # Newest first
        assert files[0]["name"] == "2026-05-14.jsonl"
        assert "size_bytes" in files[0]
        assert "modified" in files[0]

    def test_ignores_non_jsonl_files(self, tmp_path):
        (tmp_path / "2026-05-14.jsonl").write_text("x\n")
        (tmp_path / "notes.txt").write_text("not a log\n")
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            files = log_viewer.list_log_files()
        assert len(files) == 1
        assert files[0]["name"] == "2026-05-14.jsonl"


# ── read_log_tail ─────────────────────────────────────────────────────────────

class TestReadLogTail:
    def test_empty_file_returns_empty_string(self, tmp_path):
        (tmp_path / "2026-05-14.jsonl").write_text("")
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            result = log_viewer.read_log_tail("2026-05-14.jsonl")
        assert result == ""

    def test_missing_file_returns_empty_string(self, tmp_path):
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            result = log_viewer.read_log_tail("missing.jsonl")
        assert result == ""

    def test_reads_jsonl_and_formats(self, tmp_path):
        line = json.dumps({"ts": "2026-05-14 10:00:00", "kind": "tool_action", "action": "open_app"})
        (tmp_path / "2026-05-14.jsonl").write_text(line + "\n")
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            result = log_viewer.read_log_tail("2026-05-14.jsonl")
        assert "tool_action" in result
        assert "10:00:00" in result

    def test_tail_lines_limit(self, tmp_path):
        lines = [
            json.dumps({"ts": f"2026-05-14 10:0{i}:00", "kind": "event"}) + "\n"
            for i in range(20)
        ]
        (tmp_path / "2026-05-14.jsonl").write_text("".join(lines))
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            result = log_viewer.read_log_tail("2026-05-14.jsonl", tail_lines=5)
        assert result.count("event") == 5

    def test_path_traversal_raises(self, tmp_path):
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            with pytest.raises(ValueError):
                log_viewer.read_log_tail("../../../etc/passwd")

    def test_slash_in_filename_raises(self, tmp_path):
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            with pytest.raises(ValueError):
                log_viewer.read_log_tail("subdir/log.jsonl")

    def test_dotdot_in_filename_raises(self, tmp_path):
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            with pytest.raises(ValueError):
                log_viewer.read_log_tail("..log.jsonl")


# ── read_latest_log_tail ──────────────────────────────────────────────────────

class TestReadLatestLogTail:
    def test_empty_dir_returns_empty_tuple(self, tmp_path):
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            fname, text = log_viewer.read_latest_log_tail()
        assert fname == ""
        assert text == ""

    def test_missing_dir_returns_empty_tuple(self, tmp_path):
        fake = tmp_path / "nonexistent"
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", fake):
            from prometheus.infra import log_viewer
            fname, text = log_viewer.read_latest_log_tail()
        assert fname == ""

    def test_picks_newest_file(self, tmp_path):
        (tmp_path / "2026-05-13.jsonl").write_text(
            json.dumps({"ts": "2026-05-13 08:00:00", "kind": "old_event"}) + "\n"
        )
        (tmp_path / "2026-05-14.jsonl").write_text(
            json.dumps({"ts": "2026-05-14 09:00:00", "kind": "new_event"}) + "\n"
        )
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            fname, text = log_viewer.read_latest_log_tail()
        assert fname == "2026-05-14.jsonl"
        assert "new_event" in text

    def test_returns_tuple_of_strings(self, tmp_path):
        (tmp_path / "2026-05-14.jsonl").write_text(
            json.dumps({"ts": "2026-05-14 10:00:00", "kind": "x"}) + "\n"
        )
        with patch("prometheus.infra.log_viewer.JARVIS_LOGS_DIR", tmp_path):
            from prometheus.infra import log_viewer
            result = log_viewer.read_latest_log_tail()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)


# ── Safety: no subprocess ─────────────────────────────────────────────────────

class TestNoSubprocess:
    def test_no_subprocess_import(self):
        src = Path(ROOT / "prometheus" / "infra" / "log_viewer.py").read_text()
        assert "import subprocess" not in src
        assert "os.system(" not in src

    def test_no_shell_true(self):
        src = Path(ROOT / "prometheus" / "infra" / "log_viewer.py").read_text()
        assert "shell=True" not in src
