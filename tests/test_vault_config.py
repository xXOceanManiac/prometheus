"""Narrow tests for vault_path config resolution and query_vault behavior."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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
