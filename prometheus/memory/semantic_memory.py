from __future__ import annotations

from typing import Any

from prometheus.memory.memory_core import MEMORY_DIR, norm_text, now_iso, read_json, write_json

SEMANTIC_PATH = MEMORY_DIR / "semantic_memory.json"


class SemanticMemory:
    def __init__(self) -> None:
        self.path = SEMANTIC_PATH

    def read(self) -> dict[str, Any]:
        return read_json(
            self.path,
            {
                "updated_at": None,
                "facts": [],
            },
        )

    def _normalize_tags(self, tags: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for tag in tags or []:
            value = str(tag).strip()
            if not value:
                continue
            key = norm_text(value)
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return out

    def _find_record(self, data: dict[str, Any], key: str) -> dict[str, Any] | None:
        nk = norm_text(key)
        for fact in data["facts"]:
            if norm_text(fact.get("key", "")) == nk:
                return fact
        return None

    def _find(self, key: str) -> dict[str, Any] | None:
        return self._find_record(self.read(), key)

    def set_fact(
        self,
        key: str,
        value: Any,
        *,
        confidence: float = 0.8,
        source: str = "manual",
        tags: list[str] | None = None,
    ) -> None:
        data = self.read()
        existing = self._find_record(data, key)

        payload = {
            "key": str(key).strip(),
            "value": value,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "source": str(source).strip() or "manual",
            "tags": self._normalize_tags(tags),
            "updated_at": now_iso(),
        }

        if existing is None:
            data["facts"].append(payload)
        else:
            merged_tags = self._normalize_tags(
                list(existing.get("tags", [])) + payload["tags"]
            )
            existing.update(payload)
            existing["tags"] = merged_tags

        data["updated_at"] = now_iso()
        write_json(self.path, data)

    def get_fact(self, key: str) -> Any | None:
        fact = self._find(key)
        if not fact:
            return None
        return fact.get("value")

    def get_fact_record(self, key: str) -> dict[str, Any] | None:
        fact = self._find(key)
        if not fact:
            return None
        return dict(fact)

    def get_best_fact(self, key: str, min_confidence: float = 0.0) -> Any | None:
        fact = self._find(key)
        if not fact:
            return None
        if float(fact.get("confidence", 0.0)) < float(min_confidence):
            return None
        return fact.get("value")

    def get_facts_by_tag(
        self,
        tag: str,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        nt = norm_text(tag)
        matches: list[dict[str, Any]] = []
        for fact in self.read()["facts"]:
            tags = [norm_text(t) for t in fact.get("tags", [])]
            if nt in tags and float(fact.get("confidence", 0.0)) >= float(
                min_confidence
            ):
                matches.append(dict(fact))
        matches.sort(
            key=lambda f: (-float(f.get("confidence", 0.0)), str(f.get("key", "")))
        )
        return matches

    def search_facts(
        self,
        query: str,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        q = norm_text(query)
        if not q:
            return self.all_facts()

        results: list[tuple[int, dict[str, Any]]] = []
        for fact in self.read()["facts"]:
            confidence = float(fact.get("confidence", 0.0))
            if confidence < float(min_confidence):
                continue

            key = norm_text(fact.get("key", ""))
            value = norm_text(fact.get("value", ""))
            source = norm_text(fact.get("source", ""))
            tags = " ".join(norm_text(t) for t in fact.get("tags", []))

            score = 0
            for token in q.split():
                if token in key:
                    score += 5
                if token in tags:
                    score += 4
                if token in value:
                    score += 3
                if token in source:
                    score += 1

            if score > 0:
                results.append((score, dict(fact)))

        results.sort(
            key=lambda item: (
                -item[0],
                -float(item[1].get("confidence", 0.0)),
                str(item[1].get("key", "")),
            )
        )
        return [fact for _, fact in results]

    def delete_fact(self, key: str) -> bool:
        data = self.read()
        nk = norm_text(key)
        before = len(data["facts"])
        data["facts"] = [
            fact for fact in data["facts"] if norm_text(fact.get("key", "")) != nk
        ]
        changed = len(data["facts"]) != before
        if changed:
            data["updated_at"] = now_iso()
            write_json(self.path, data)
        return changed

    def all_facts(self) -> list[dict[str, Any]]:
        facts = [dict(fact) for fact in self.read()["facts"]]
        facts.sort(
            key=lambda f: (-float(f.get("confidence", 0.0)), str(f.get("key", "")))
        )
        return facts
