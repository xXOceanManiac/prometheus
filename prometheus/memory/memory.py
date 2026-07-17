from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from prometheus.infra.config import BASE_DIR, LOG_DIR


MEMORY_PATH = BASE_DIR / "memory.json"


def _norm(value: str) -> str:
    value = str(value).strip().lower()
    for ch in ["_", "-", "/", "."]:
        value = value.replace(ch, " ")
    return " ".join(value.split())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or MEMORY_PATH
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._default_data())
        self.import_logs_if_needed()

    def _default_data(self) -> dict[str, Any]:
        now = _now()
        return {
            "version": 2,
            "updated_at": now,
            "last_context_name": None,
            "contexts": [],
            "routines": [],
        }

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("version", 2)
                data.setdefault("updated_at", _now())
                data.setdefault("last_context_name", None)
                data.setdefault("contexts", [])
                data.setdefault("routines", [])
                return data
        except Exception:
            pass
        return self._default_data()

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _now()
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _unique_list(self, values: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            value = str(item).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return out

    def _merge_context_data(
        self, base: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        base["apps"] = self._unique_list(
            list(base.get("apps", [])) + list(incoming.get("apps", []))
        )
        base["url_keys"] = self._unique_list(
            list(base.get("url_keys", [])) + list(incoming.get("url_keys", []))
        )
        base["urls"] = self._unique_list(
            list(base.get("urls", [])) + list(incoming.get("urls", []))
        )
        base["tags"] = self._unique_list(
            list(base.get("tags", [])) + list(incoming.get("tags", []))
        )

        if str(incoming.get("project_path", "")).strip():
            base["project_path"] = str(incoming["project_path"]).strip()
        if str(incoming.get("notes", "")).strip():
            base["notes"] = str(incoming["notes"]).strip()
        if str(incoming.get("layout", "")).strip():
            base["layout"] = str(incoming["layout"]).strip()
        if str(incoming.get("source", "")).strip():
            base["source"] = str(incoming["source"]).strip()

        return base

    def _extract_actions(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        actions = payload.get("actions")
        if isinstance(actions, list) and actions:
            return [a for a in actions if isinstance(a, dict)]
        if isinstance(payload, dict) and payload.get("action"):
            return [payload]
        return []

    def _context_from_payload(
        self, payload: dict[str, Any], ts: str
    ) -> dict[str, Any] | None:
        actions = self._extract_actions(payload)
        if not actions:
            return None

        ctx = {
            "name": "",
            "project_path": "",
            "apps": [],
            "url_keys": [],
            "urls": [],
            "notes": "",
            "tags": [],
            "layout": "",
            "source": "log_import",
            "ts": ts,
        }

        for action in actions:
            kind = str(action.get("action", "")).strip()

            if kind == "open_app" and action.get("app"):
                ctx["apps"].append(str(action["app"]).strip().lower())

            elif kind == "open_url_key" and action.get("url_key"):
                ctx["url_keys"].append(str(action["url_key"]).strip().lower())

            elif kind == "open_url_keys":
                ctx["url_keys"].extend(
                    str(x).strip().lower()
                    for x in action.get("url_keys", [])
                    if str(x).strip()
                )

            elif kind == "open_url_raw" and action.get("url"):
                ctx["urls"].append(str(action["url"]).strip())

            elif kind in {"open_code_folder", "open_terminal_here"} and action.get(
                "project_path"
            ):
                ctx["project_path"] = str(action["project_path"]).strip()

            elif kind == "run_routine" and action.get("routine_name"):
                ctx["name"] = str(action["routine_name"]).strip()
                ctx["notes"] = f"Imported routine session from {ts}"

            elif kind == "save_context" and action.get("context_name"):
                ctx["name"] = str(action["context_name"]).strip()

        ctx["apps"] = self._unique_list(ctx["apps"])
        ctx["url_keys"] = self._unique_list(ctx["url_keys"])
        ctx["urls"] = self._unique_list(ctx["urls"])

        if not (ctx["apps"] or ctx["url_keys"] or ctx["urls"] or ctx["project_path"]):
            return None

        if not ctx["name"]:
            if ctx["project_path"]:
                ctx["name"] = Path(ctx["project_path"]).name
            elif ctx["url_keys"]:
                ctx["name"] = f"Imported {' '.join(ctx['url_keys'][:2])}"
            else:
                ctx["name"] = f"Imported session {ts}"

        return ctx

    def import_logs_if_needed(self) -> int:
        with self._lock:
            data = self._read()
            if data["contexts"]:
                return 0
            imported = self.backfill_from_logs_locked(data)
            if imported:
                self._write(data)
            return imported

    def backfill_from_logs(self) -> int:
        with self._lock:
            data = self._read()
            imported = self.backfill_from_logs_locked(data)
            if imported:
                self._write(data)
            return imported

    def backfill_from_logs_locked(self, data: dict[str, Any]) -> int:
        sessions: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        last_epoch: float | None = None

        for path in sorted(LOG_DIR.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue

                        if rec.get("kind") != "tool_execute":
                            continue

                        payload = rec.get("payload", {})
                        ts = str(rec.get("ts", "")).strip()
                        extracted = self._context_from_payload(payload, ts)
                        if not extracted:
                            continue

                        try:
                            epoch = datetime.strptime(
                                ts, "%Y-%m-%d %H:%M:%S"
                            ).timestamp()
                        except Exception:
                            epoch = time.time()

                        if (
                            current is None
                            or last_epoch is None
                            or (epoch - last_epoch) > 600
                        ):
                            current = {
                                "name": extracted["name"],
                                "project_path": extracted["project_path"],
                                "apps": list(extracted["apps"]),
                                "url_keys": list(extracted["url_keys"]),
                                "urls": list(extracted["urls"]),
                                "notes": extracted["notes"],
                                "tags": list(extracted["tags"]),
                                "layout": extracted["layout"],
                                "source": "log_import",
                                "ts": ts,
                            }
                            sessions.append(current)
                        else:
                            current = self._merge_context_data(current, extracted)
                            if extracted.get("name"):
                                current["name"] = extracted["name"]

                        last_epoch = epoch
            except Exception:
                continue

        if not sessions:
            return 0

        contexts = data["contexts"]
        now = _now()
        imported_count = 0

        for idx, sess in enumerate(sessions, start=1):
            name = str(sess.get("name", "")).strip() or f"Imported session {idx}"
            existing = None
            for ctx in contexts:
                if str(ctx.get("name", "")).strip().lower() == name.lower():
                    existing = ctx
                    break

            if existing is None:
                ctx = {
                    "id": _slug(name) or f"context-{len(contexts)+1}",
                    "name": name,
                    "created_at": now,
                    "updated_at": now,
                    "last_used_at": now,
                    "use_count": 0,
                    "project_path": str(sess.get("project_path", "")).strip(),
                    "apps": self._unique_list(sess.get("apps", [])),
                    "url_keys": self._unique_list(sess.get("url_keys", [])),
                    "urls": self._unique_list(sess.get("urls", [])),
                    "notes": str(sess.get("notes", "")).strip(),
                    "tags": self._unique_list(sess.get("tags", [])),
                    "layout": str(sess.get("layout", "")).strip(),
                    "source": "log_import",
                }
                contexts.append(ctx)
                imported_count += 1
            else:
                self._merge_context_data(existing, sess)

        if contexts and not data.get("last_context_name"):
            data["last_context_name"] = contexts[-1]["name"]

        return imported_count

    def remember_context(
        self,
        *,
        name: str,
        project_path: str = "",
        apps: list[str] | None = None,
        url_keys: list[str] | None = None,
        urls: list[str] | None = None,
        notes: str = "",
        tags: list[str] | None = None,
        layout: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        name = str(name).strip()
        if not name:
            raise ValueError("Context name is required.")

        with self._lock:
            data = self._read()
            contexts = data["contexts"]

            existing = None
            for ctx in contexts:
                if str(ctx.get("name", "")).strip().lower() == name.lower():
                    existing = ctx
                    break

            now = _now()
            apps_clean = self._unique_list(apps)
            url_keys_clean = self._unique_list(url_keys)
            urls_clean = self._unique_list(urls)
            tags_clean = self._unique_list(tags)

            if existing is None:
                ctx = {
                    "id": _slug(name) or f"context-{len(contexts)+1}",
                    "name": name,
                    "created_at": now,
                    "updated_at": now,
                    "last_used_at": now,
                    "use_count": 1,
                    "project_path": project_path.strip(),
                    "apps": apps_clean,
                    "url_keys": url_keys_clean,
                    "urls": urls_clean,
                    "notes": str(notes).strip(),
                    "tags": tags_clean,
                    "layout": str(layout).strip(),
                    "source": source,
                }
                contexts.append(ctx)
                existing = ctx
            else:
                existing["updated_at"] = now
                existing["last_used_at"] = now
                existing["use_count"] = int(existing.get("use_count", 0)) + 1

                if project_path.strip():
                    existing["project_path"] = project_path.strip()

                existing["apps"] = self._unique_list(
                    list(existing.get("apps", [])) + apps_clean
                )
                existing["url_keys"] = self._unique_list(
                    list(existing.get("url_keys", [])) + url_keys_clean
                )
                existing["urls"] = self._unique_list(
                    list(existing.get("urls", [])) + urls_clean
                )
                existing["tags"] = self._unique_list(
                    list(existing.get("tags", [])) + tags_clean
                )

                if str(notes).strip():
                    existing["notes"] = str(notes).strip()
                if str(layout).strip():
                    existing["layout"] = str(layout).strip()
                if source:
                    existing["source"] = source

            data["last_context_name"] = existing["name"]
            self._write(data)
            return existing

    def touch_context(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            now = _now()
            for ctx in data["contexts"]:
                if str(ctx.get("name", "")).strip().lower() == name.strip().lower():
                    ctx["last_used_at"] = now
                    ctx["updated_at"] = now
                    ctx["use_count"] = int(ctx.get("use_count", 0)) + 1
                    data["last_context_name"] = ctx["name"]
                    self._write(data)
                    return ctx
        return None

    def get_last_context(self) -> dict[str, Any] | None:
        data = self._read()
        contexts = data.get("contexts", [])

        name = data.get("last_context_name")
        if name:
            ctx = self.get_context(str(name))
            if ctx and (
                str(ctx.get("project_path", "")).strip()
                or ctx.get("url_keys")
                or ctx.get("apps")
            ):
                return ctx

        ranked = sorted(
            contexts,
            key=lambda c: (
                1 if str(c.get("project_path", "")).strip() else 0,
                len(c.get("url_keys", [])),
                len(c.get("apps", [])),
                int(c.get("use_count", 0)),
                str(c.get("last_used_at", "")),
            ),
            reverse=True,
        )

        return ranked[0] if ranked else None

    def get_context(self, name: str) -> dict[str, Any] | None:
        data = self._read()
        exact = None
        partial = None
        q = name.strip().lower()
        if not q:
            return None

        for ctx in data["contexts"]:
            ctx_name = str(ctx.get("name", "")).strip()
            if ctx_name.lower() == q:
                exact = ctx
                break
            hay = " ".join(
                [
                    ctx_name,
                    str(ctx.get("project_path", "")),
                    str(ctx.get("notes", "")),
                    " ".join(ctx.get("tags", [])),
                    " ".join(ctx.get("apps", [])),
                    " ".join(ctx.get("url_keys", [])),
                ]
            ).lower()
            if q in hay and partial is None:
                partial = ctx

        return exact or partial

    def _score_context(self, ctx: dict[str, Any], query: str) -> int:
        q = query.strip().lower()
        if not q:
            return -1

        score = 0
        name = str(ctx.get("name", "")).lower()
        notes = str(ctx.get("notes", "")).lower()
        project_path = str(ctx.get("project_path", "")).lower()
        tags = " ".join(str(t).lower() for t in ctx.get("tags", []))
        apps = " ".join(str(a).lower() for a in ctx.get("apps", []))
        url_keys = " ".join(str(k).lower() for k in ctx.get("url_keys", []))
        urls = " ".join(str(u).lower() for u in ctx.get("urls", []))

        for token in q.split():
            if token in name:
                score += 7
            if token in project_path:
                score += 6
            if token in tags:
                score += 5
            if token in notes:
                score += 3
            if token in apps:
                score += 2
            if token in url_keys:
                score += 2
            if token in urls:
                score += 1

        if str(ctx.get("project_path", "")).strip():
            score += 3

        score += min(int(ctx.get("use_count", 0)), 10)

        if (
            str(ctx.get("source", "")) == "log_import"
            and not str(ctx.get("project_path", "")).strip()
        ):
            score -= 4

        generic_apps = {"chrome", "google chrome", "browser"}
        apps_set = {str(a).lower() for a in ctx.get("apps", [])}
        if apps_set and apps_set.issubset(
            generic_apps | {"code", "visual studio code"}
        ):
            score -= 2

        generic_url_keys = {"chatgpt", "google"}
        url_key_set = {str(k).lower() for k in ctx.get("url_keys", [])}
        if url_key_set and url_key_set.issubset(generic_url_keys):
            score -= 2

        return score

    def search_best_context(self, query: str) -> dict[str, Any] | None:
        data = self._read()
        q = query.strip().lower()
        if not q:
            return self.get_last_context()

        best = None
        best_score = -10_000

        for ctx in data["contexts"]:
            score = self._score_context(ctx, q)
            if score > best_score:
                best_score = score
                best = ctx

        if best_score <= 0:
            return self.get_last_context()

        return best

    def save_routine(
        self,
        *,
        name: str,
        steps: list[dict[str, Any]],
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        name = str(name).strip()
        if not name:
            raise ValueError("Routine name is required.")

        with self._lock:
            data = self._read()
            routines = data["routines"]
            existing = None

            for routine in routines:
                if str(routine.get("name", "")).strip().lower() == name.lower():
                    existing = routine
                    break

            now = _now()
            payload = {
                "id": _slug(name) or f"routine-{len(routines)+1}",
                "name": name,
                "description": str(description).strip(),
                "tags": self._unique_list(tags),
                "steps": steps,
                "created_at": now,
                "updated_at": now,
                "last_used_at": now,
                "use_count": 0,
            }

            if existing is None:
                routines.append(payload)
                existing = payload
            else:
                existing.update(payload)
                existing["created_at"] = existing.get("created_at", now)

            self._write(data)
            return existing

    def get_routine(self, name: str) -> dict[str, Any] | None:
        data = self._read()
        q = _norm(name)
        if not q:
            return None

        exact = None
        partial = None

        for routine in data["routines"]:
            routine_name = str(routine.get("name", "")).strip()
            routine_norm = _norm(routine_name)
            hay = _norm(
                " ".join(
                    [
                        routine_name,
                        str(routine.get("description", "")),
                        " ".join(routine.get("tags", [])),
                    ]
                )
            )

            if routine_norm == q:
                exact = routine
                break

            if q in hay and partial is None:
                partial = routine

        return exact or partial

    def touch_routine(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            now = _now()
            for routine in data["routines"]:
                if str(routine.get("name", "")).strip().lower() == name.strip().lower():
                    routine["last_used_at"] = now
                    routine["updated_at"] = now
                    routine["use_count"] = int(routine.get("use_count", 0)) + 1
                    self._write(data)
                    return routine
        return None

    def build_actions_from_context(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        project_path = str(ctx.get("project_path", "")).strip()
        apps = [str(x).strip().lower() for x in ctx.get("apps", []) if str(x).strip()]
        url_keys = [
            str(x).strip().lower() for x in ctx.get("url_keys", []) if str(x).strip()
        ]
        urls = [str(x).strip() for x in ctx.get("urls", []) if str(x).strip()]

        if project_path:
            actions.append({"action": "open_code_folder", "project_path": project_path})
            actions.append(
                {"action": "open_terminal_here", "project_path": project_path}
            )

        skip = {"code", "vscode", "terminal"}
        for app in apps:
            if project_path and app in skip:
                continue
            actions.append({"action": "open_app", "app": app})

        if url_keys:
            actions.append({"action": "open_url_keys", "url_keys": url_keys})

        for url in urls:
            actions.append({"action": "open_url_raw", "url": url})

        return actions
