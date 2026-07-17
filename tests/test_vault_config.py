"""Narrow tests for vault_path config resolution and query_vault behavior."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

_VAULT_PATH = "/home/tatel/Desktop/Tates Brain"
_VAULT_DB = Path(_VAULT_PATH) / "data" / "memory.db"

vault_available = pytest.mark.skipif(
    not _VAULT_DB.exists(),
    reason=f"vault db not present at {_VAULT_DB}",
)


class TestQueryVaultGuards:
    """query_vault must never raise and must return [] for unconfigured/invalid inputs."""

    def test_empty_path_returns_empty_list(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": ""}):
            assert query_vault("prometheus") == []

    def test_whitespace_path_returns_empty_list(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": "   "}):
            assert query_vault("prometheus") == []

    def test_missing_db_returns_empty_list(self, tmp_path):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": str(tmp_path)}):
            assert query_vault("prometheus") == []

    def test_empty_query_returns_empty_list(self, tmp_path):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": str(tmp_path)}):
            assert query_vault("") == []

    def test_nonexistent_path_returns_empty_list(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": "/nonexistent/path/that/does/not/exist"}):
            assert query_vault("prometheus") == []


class TestVaultPathConfigured:
    """Pass 1 regression: vault_path must be non-empty and the db must exist."""

    def test_vault_path_is_set_in_runtime_config(self):
        from prometheus.infra.config import CONFIG

        vault_path = str(CONFIG.get("vault_path", "")).strip()
        assert vault_path, (
            "vault_path is empty in ~/.jarvis/config.json — Pass 1 fix not applied"
        )

    def test_vault_path_directory_exists(self):
        from prometheus.infra.config import CONFIG

        vault_path = str(CONFIG.get("vault_path", "")).strip()
        if not vault_path:
            pytest.skip("vault_path not configured")
        assert Path(vault_path).exists(), f"vault directory not found: {vault_path}"

    def test_vault_db_exists(self):
        from prometheus.infra.config import CONFIG

        vault_path = str(CONFIG.get("vault_path", "")).strip()
        if not vault_path:
            pytest.skip("vault_path not configured")
        db = Path(vault_path) / "data" / "memory.db"
        assert db.exists(), f"vault db not found at {db}"


class TestQueryVaultLive:
    """Live query tests — skipped if vault db is not present on this machine."""

    @vault_available
    def test_returns_list(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": _VAULT_PATH}):
            results = query_vault("prometheus assistant memory", limit=3)
        assert isinstance(results, list)

    @vault_available
    def test_returns_results_for_known_topic(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": _VAULT_PATH}):
            results = query_vault("python project assistant", limit=3)
        assert len(results) > 0, "vault returned no results for a broad query"

    @vault_available
    def test_result_has_required_keys(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": _VAULT_PATH}):
            results = query_vault("assistant project", limit=2)
        for r in results:
            assert "chunk_id" in r, f"missing chunk_id: {r}"
            assert "text" in r, f"missing text: {r}"

    @vault_available
    def test_no_duplicate_chunk_ids(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": _VAULT_PATH}):
            results = query_vault("the and is a", limit=5)
        ids = [r["chunk_id"] for r in results if r.get("chunk_id")]
        assert len(ids) == len(set(ids)), "duplicate chunk_ids in results"

    @vault_available
    def test_limit_respected(self):
        from prometheus.memory.memory_core import query_vault

        with patch("prometheus.memory.memory_core.CONFIG", {"vault_path": _VAULT_PATH}):
            results = query_vault("python assistant memory project", limit=2)
        assert len(results) <= 2
