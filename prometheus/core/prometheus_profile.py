"""
prometheus_profile.py — Personal profile and daily pattern loader for Prometheus.

Reads static config values, recent vault sessions, working memory patterns, and
vault-derived priorities. Caches to ~/.jarvis/profile_cache.json once per day.
Never raises.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheus.infra.config import CONFIG
from prometheus.infra.utils import log_event

_PROFILE_CACHE_PATH = Path.home() / ".jarvis" / "profile_cache.json"


@dataclass
class UserProfile:
    name: str = "Tate"
    timezone: str = "America/New_York"
    working_style: str = "systems thinker, builds toward leverage"
    active_projects: list[str] = field(default_factory=list)
    current_priorities: list[str] = field(default_factory=list)
    preferred_response_style: str = "direct, short, no preamble"
    daily_patterns: dict = field(default_factory=dict)
    recurring_routines: list[str] = field(default_factory=list)
    faith_fitness_legacy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "timezone": self.timezone,
            "working_style": self.working_style,
            "active_projects": self.active_projects,
            "current_priorities": self.current_priorities,
            "preferred_response_style": self.preferred_response_style,
            "daily_patterns": self.daily_patterns,
            "recurring_routines": self.recurring_routines,
            "faith_fitness_legacy": self.faith_fitness_legacy,
        }


class PrometheusProfile:
    """
    Builds and caches a UserProfile once per calendar day.
    All methods fail silently — never raises to caller.
    """

    def load(self) -> UserProfile:
        """
        Return cached profile if built today, otherwise rebuild and cache.
        Never raises.
        """
        try:
            cached = self._read_cache()
            if cached is not None:
                return cached
        except Exception:
            pass

        profile = self._build()
        try:
            self._write_cache(profile)
        except Exception:
            pass
        return profile

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _read_cache(self) -> UserProfile | None:
        """Return cached profile if it was built today, else None."""
        if not _PROFILE_CACHE_PATH.exists():
            return None
        try:
            data = json.loads(_PROFILE_CACHE_PATH.read_text(encoding="utf-8"))
            built_at = str(data.get("built_at", ""))
            today = time.strftime("%Y-%m-%d")
            if built_at[:10] == today:
                profile_data = data.get("profile", {})
                return UserProfile(**{
                    k: v for k, v in profile_data.items()
                    if k in UserProfile.__dataclass_fields__
                })
        except Exception:
            pass
        return None

    def _write_cache(self, profile: UserProfile) -> None:
        _PROFILE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROFILE_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "profile": profile.to_dict(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        import os
        os.replace(tmp, _PROFILE_CACHE_PATH)

    # ------------------------------------------------------------------
    # Profile construction
    # ------------------------------------------------------------------

    def _build(self) -> UserProfile:
        profile = UserProfile()
        try:
            profile.active_projects = self._load_active_projects()
        except Exception:
            pass
        try:
            profile.current_priorities = self._load_priorities()
        except Exception:
            pass
        try:
            profile.daily_patterns = self._load_daily_patterns()
        except Exception:
            pass
        try:
            profile.recurring_routines = self._load_routines()
        except Exception:
            pass
        log_event("profile_built", {
            "active_projects": profile.active_projects,
            "priorities": profile.current_priorities,
        })
        return profile

    def _load_active_projects(self) -> list[str]:
        """Scan last 5 vault session files for active_project in YAML frontmatter."""
        vault_path_str = str(CONFIG.get("vault_path", "")).strip()
        if not vault_path_str:
            return []
        sessions_dir = Path(vault_path_str).expanduser() / "vault" / "Sessions"
        if not sessions_dir.is_dir():
            return []

        # Find most recent year directory
        year_dirs = sorted(
            [d for d in sessions_dir.iterdir() if d.is_dir() and d.name.isdigit()],
            reverse=True,
        )
        if not year_dirs:
            return []

        session_files = sorted(year_dirs[0].glob("*.md"), reverse=True)[:5]
        projects: list[str] = []
        for f in session_files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                # Parse YAML frontmatter
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end > 0:
                        fm = text[3:end]
                        for line in fm.splitlines():
                            if line.startswith("active_project:"):
                                value = line.split(":", 1)[1].strip().strip('"').strip("'")
                                if value and value not in projects and value != "unknown":
                                    projects.append(value)
                                break
            except Exception:
                continue
        return projects[:5]

    def _load_priorities(self) -> list[str]:
        """Query vault for current-month priorities and goals."""
        try:
            from prometheus.memory.memory_core import query_vault
            month = time.strftime("%B")
            results = query_vault(f"priorities goals {month}", limit=5)
            priorities: list[str] = []
            for r in results[:3]:
                title = str(r.get("title") or "").strip()
                text = str(r.get("text") or "")
                first_sentence = text.split(".")[0][:100].strip() if text else ""
                label = title or first_sentence
                if label and label not in priorities:
                    priorities.append(label)
            return priorities
        except Exception:
            return []

    def _load_daily_patterns(self) -> dict:
        """Read patterns straight from working_memory.json."""
        try:
            from prometheus.memory.memory_core import MEMORY_DIR, read_json
            wm = read_json(MEMORY_DIR / "working_memory.json", {})
            patterns: dict = {}
            # Extract any pattern-like fields stored in working memory
            for key in ("preferred_work_hours", "common_apps", "daily_patterns", "patterns"):
                val = wm.get(key)
                if val is not None:
                    patterns[key] = val
            return patterns
        except Exception:
            return {}

    def _load_routines(self) -> list[str]:
        """Load routine names from CONFIG."""
        try:
            routines = CONFIG.get("routines", {})
            if isinstance(routines, dict):
                return list(routines.keys())[:10]
        except Exception:
            pass
        return []
