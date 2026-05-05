from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import CONFIG
from semantic_memory import SemanticMemory
from procedural_memory import ProceduralMemory
from working_memory import WorkingMemory
from memory_core import norm_text


@dataclass
class BrainPlan:
    confidence: float
    intent: str
    reason: str
    steps: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "intent": self.intent,
            "reason": self.reason,
            "steps": self.steps,
        }


class PrometheusBrain:
    def __init__(
        self,
        *,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
        working: WorkingMemory,
    ) -> None:
        self.semantic = semantic
        self.procedural = procedural
        self.working = working

    def _trigger_fact_key(self, phrase: str) -> str:
        return f"preference_trigger::{norm_text(phrase)}"

    def save_preference(
        self,
        *,
        trigger_phrase: str,
        steps: list[dict[str, Any]] | None = None,
        routine_name: str = "",
        script_name: str = "",
        intent_name: str = "",
        notes: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        phrase = str(trigger_phrase).strip()
        if not phrase:
            raise ValueError("trigger_phrase is required")

        clean_steps = [dict(step) for step in (steps or []) if isinstance(step, dict)]
        payload = {
            "trigger_phrase": phrase,
            "routine_name": str(routine_name).strip(),
            "script_name": str(script_name).strip(),
            "intent_name": str(intent_name).strip(),
            "notes": str(notes).strip(),
            "steps": clean_steps,
        }
        self.semantic.set_fact(
            self._trigger_fact_key(phrase),
            payload,
            confidence=0.98,
            source=source,
            tags=["preference", "trigger", "prometheus_v3"],
        )

        if clean_steps:
            chosen_name = payload["routine_name"] or f"preference::{phrase}"
            self.procedural.save_routine(
                chosen_name,
                description=payload["notes"] or f"Preferred Prometheus behavior for '{phrase}'.",
                triggers=[phrase],
                steps=clean_steps,
                tags=["preference", "prometheus_v3"],
            )
            payload["routine_name"] = chosen_name

        return payload

    def _lookup_trigger(self, text: str) -> dict[str, Any] | None:
        normalized = norm_text(text)
        exact = self.semantic.get_fact(self._trigger_fact_key(normalized))
        if isinstance(exact, dict):
            return exact

        for fact in self.semantic.get_facts_by_tag("trigger", min_confidence=0.75):
            value = fact.get("value")
            if not isinstance(value, dict):
                continue
            phrase = norm_text(value.get("trigger_phrase", ""))
            if not phrase:
                continue
            if normalized == phrase or normalized.startswith(phrase) or phrase in normalized:
                return value
        return None

    def _choose_config_routine(self, text: str) -> tuple[str, float] | tuple[None, float]:
        normalized = norm_text(text)
        best_key = None
        best_score = 0
        for key, item in CONFIG.get("routines", {}).items():
            hay = " ".join(
                [key, str(item.get("description", ""))]
            )
            hay_norm = norm_text(hay)
            score = 0
            for token in normalized.split():
                if token in hay_norm:
                    score += 1
            if hay_norm and normalized and (normalized in hay_norm or hay_norm in normalized):
                score += 3
            if score > best_score:
                best_key = key
                best_score = score
        if best_key and best_score >= 2:
            return best_key, min(0.91, 0.55 + best_score * 0.06)
        return None, 0.0

    def suggest_plan(self, request_text: str) -> BrainPlan:
        text = str(request_text or "").strip()
        normalized = norm_text(text)
        if not normalized:
            return BrainPlan(0.0, "none", "Empty request.", [])

        pref = self._lookup_trigger(normalized)
        if pref:
            steps = [dict(step) for step in pref.get("steps", []) if isinstance(step, dict)]
            routine_name = str(pref.get("routine_name", "")).strip()
            script_name = str(pref.get("script_name", "")).strip()
            if steps:
                return BrainPlan(0.99, pref.get("intent_name") or "preference", "Matched stored trigger preference.", steps)
            if routine_name:
                return BrainPlan(0.98, pref.get("intent_name") or "preference", "Matched stored trigger preference.", [
                    {"action": "run_routine", "routine_name": routine_name}
                ])
            if script_name:
                return BrainPlan(0.98, pref.get("intent_name") or "preference", "Matched stored trigger preference.", [
                    {"action": "run_ha_script", "script_name": script_name}
                ])

        # routine preference from procedural memory
        proc = self.procedural.get_routine(normalized)
        if proc and proc.get("steps"):
            return BrainPlan(0.94, "routine", "Matched learned routine.", [
                {"action": "run_routine", "routine_name": str(proc.get("name", normalized))}
            ])

        # focused heuristics
        gaming_words = {"xbox", "game", "gaming", "netflix", "movie", "cinematic", "lights"}
        focus_words = {"lock in", "focus", "work session", "lets work", "get some work done", "deep work"}
        screen_words = {"screen", "window", "focused", "focus", "what am i looking at", "what do i have open"}
        correction_words = {"next time", "instead", "from now on", "when i say"}

        if any(phrase in normalized for phrase in correction_words):
            return BrainPlan(0.2, "correction", "Likely a correction request; the model should store a preference.", [])

        if any(phrase in normalized for phrase in screen_words):
            if "open" in normalized or "windows" in normalized:
                return BrainPlan(0.95, "desktop_awareness", "Screen-awareness request.", [{"action": "list_windows"}])
            return BrainPlan(0.95, "desktop_awareness", "Screen-awareness request.", [{"action": "desktop_state"}])

        if any(token in normalized for token in gaming_words):
            if "netflix" in normalized:
                return BrainPlan(0.93, "media_netflix", "Detected Netflix/Xbox request.", [
                    {"action": "run_ha_script", "script_name": "netflix_on_xbox"}
                ])
            if "movie" in normalized or "cinematic" in normalized:
                return BrainPlan(0.93, "media_movie", "Detected movie/cinematic request.", [
                    {"action": "run_ha_script", "script_name": "movie_mode_full"}
                ])

        if any(phrase in normalized for phrase in focus_words):
            custom_routine = self.procedural.get_routine("lock in") or self.procedural.get_routine("focus")
            if custom_routine:
                return BrainPlan(0.9, "focus", "Matched learned focus routine.", [
                    {"action": "run_routine", "routine_name": str(custom_routine.get("name", "lock in"))}
                ])
            return BrainPlan(0.82, "focus", "Detected focus/work intent.", [{"action": "mode_lock_in"}])

        routine_key, routine_conf = self._choose_config_routine(normalized)
        if routine_key:
            return BrainPlan(routine_conf, "routine", "Matched configured routine by similarity.", [
                {"action": "run_routine", "routine_name": routine_key}
            ])

        # desktop resume hints
        resume_phrases = (
            "continue", "resume", "same as yesterday", "same as before", "restore", "pick back up"
        )
        if any(p in normalized for p in resume_phrases):
            return BrainPlan(0.78, "resume", "Detected resume/restore intent.", [
                {"action": "resume_last_context", "query": text}
            ])

        return BrainPlan(0.0, "none", "No confident heuristic match.", [])
