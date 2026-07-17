from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from prometheus.infra.config import BASE_DIR, CONFIG

MEMORY_DIR = BASE_DIR / "memory_v2"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def norm_text(value: str) -> str:
    value = str(value or "").strip().lower()
    for ch in ["_", "-", "/", "."]:
        value = value.replace(ch, " ")
    return " ".join(value.split())


def _fts5_query(text: str) -> str:
    """Build a safe FTS5 MATCH expression from free-form text."""
    tokens = [t for t in text.split() if len(t) > 1][:12]
    if not tokens:
        return ""
    # Quote each token to neutralise FTS5 special chars, OR them for broad recall.
    quoted = ['"' + t.replace('"', "") + '"' for t in tokens]
    return " OR ".join(quoted)


def query_vault(text: str, limit: int = 5) -> list[dict]:
    """
    Full-text search the SQLite FTS5 vault database.

    The vault database lives at:  CONFIG['vault_path'] + '/data/memory.db'

    Returns a list of dicts with keys: chunk_id, title, year, text, topics.
    Returns an empty list (never raises) if:
      - vault_path is not configured
      - the database file does not exist
      - any query or schema error occurs
    """
    vault_path_str = str(CONFIG.get("vault_path", "")).strip()
    if not vault_path_str:
        return []

    db_path = Path(vault_path_str).expanduser() / "data" / "memory.db"
    if not db_path.exists():
        write_json(
            MEMORY_DIR / "vault_warnings.json",
            {"warning": f"vault db not found at {db_path}", "ts": now_iso()},
        )
        return []

    query = _fts5_query(str(text or "").strip())
    if not query:
        return []

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT chunk_id, title, year, text, topics
            FROM memory_fts
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, int(limit) * 4),
        )
        rows = cur.fetchall()
        conn.close()
        # Deduplicate: first by chunk_id, then collapse same-title conversations
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            cid = str(d.get("chunk_id") or "")
            title_key = str(d.get("title") or "").strip().lower()
            if cid and cid in seen_ids:
                continue
            if title_key and title_key in seen_titles:
                continue
            if cid:
                seen_ids.add(cid)
            if title_key:
                seen_titles.add(title_key)
            results.append(d)
            if len(results) >= limit:
                break
        return results
    except sqlite3.OperationalError as exc:
        # Table or column names may differ — log and continue silently.
        write_json(
            MEMORY_DIR / "vault_warnings.json",
            {"warning": f"vault query error: {exc}", "ts": now_iso()},
        )
        return []
    except Exception as exc:
        write_json(
            MEMORY_DIR / "vault_warnings.json",
            {"warning": f"vault unexpected error: {exc}", "ts": now_iso()},
        )
        return []
