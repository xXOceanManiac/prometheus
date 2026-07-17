from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any


class BehaviorLearningEngine:
    def __init__(self, memory, semantic, procedural, working, episodes) -> None:
        self.memory = memory
        self.semantic = semantic
        self.procedural = procedural
        self.working = working
        self.episodes = episodes

    def _norm(self, value: str) -> str:
        value = str(value or "").strip().lower()
        for ch in ["_", "-", "/", ".", ":", "\\"]:
            value = value.replace(ch, " ")
        return " ".join(value.split())

    def _slug(self, value: str) -> str:
        return "-".join(self._norm(value).split())

    def _read_working(self) -> dict[str, Any]:
        try:
            return self.working.read()
        except Exception:
            return {}

    def _maybe_existing_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        p = Path(raw).expanduser()
        return str(p) if p.exists() else ""

    def _all_contexts(self) -> list[dict[str, Any]]:
        try:
            data = self.memory._read()  # type: ignore[attr-defined]
            return list(data.get("contexts", []))
        except Exception:
            return []

    def _find_context_by_name(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        try:
            return self.memory.get_context(name)
        except Exception:
            return None

    def _cwd_from_pid(self, pid_value: Any) -> str:
        try:
            pid = int(pid_value)
        except Exception:
            return ""
        proc_path = Path(f"/proc/{pid}/cwd")
        try:
            resolved = proc_path.resolve()
        except Exception:
            return ""
        return self._maybe_existing_path(str(resolved))

    def _paths_from_window_title(self, title: str) -> list[str]:
        out: list[str] = []
        q = self._norm(title)
        if not q:
            return out
        for ctx in self._all_contexts():
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            if not proj:
                continue
            name = self._norm(ctx.get("name", ""))
            base = self._norm(Path(proj).name)
            if (name and name in q) or (base and base in q):
                out.append(proj)
        return out

    def resolve_active_project(
        self,
        desktop_state: dict[str, Any] | None = None,
        request_text: str = "",
    ) -> dict[str, Any]:
        """
        Resolution priority:
        1) explicit mention in current request
        2) active window PID cwd (terminal / code child process)
        3) active context / workspace from working memory
        4) active window title match
        5) last plan / last tool result project path
        6) most recently used context with a valid path
        """
        working = self._read_working()
        candidates: list[tuple[str, str, float, str]] = []

        req = self._norm(
            request_text
            or " ".join(
                [
                    str(working.get("last_user_request", "")),
                    str(working.get("last_user_transcript", "")),
                ]
            )
        )

        # 1) explicit current request mention
        if req:
            for ctx in self._all_contexts():
                proj = self._maybe_existing_path(ctx.get("project_path", ""))
                if not proj:
                    continue
                ctx_name = self._norm(ctx.get("name", ""))
                proj_name = self._norm(Path(proj).name)
                if (ctx_name and ctx_name in req) or (proj_name and proj_name in req):
                    candidates.append(
                        (
                            proj,
                            str(ctx.get("name", "")).strip() or Path(proj).name,
                            1.00,
                            "request_text",
                        )
                    )

        # 2) active PID cwd
        if desktop_state:
            active = desktop_state.get("active_window", {}) or {}
            cwd = self._cwd_from_pid(active.get("pid"))
            if cwd:
                candidates.append((cwd, Path(cwd).name, 0.99, "active_pid_cwd"))

        # 3) active context / workspace
        for label in [
            str(working.get("active_context_name", "")).strip(),
            str(working.get("active_workspace", "")).strip(),
        ]:
            if not label or label.lower().startswith("session "):
                continue
            ctx = self._find_context_by_name(label)
            if ctx:
                proj = self._maybe_existing_path(ctx.get("project_path", ""))
                if proj:
                    candidates.append(
                        (
                            proj,
                            str(ctx.get("name", "")).strip() or Path(proj).name,
                            0.96,
                            "working_context",
                        )
                    )

        # 4) active window title match
        if desktop_state:
            title = str(
                (desktop_state.get("active_window") or {}).get("title", "")
            ).strip()
            for proj in self._paths_from_window_title(title):
                candidates.append((proj, Path(proj).name, 0.92, "window_title"))

        # 5) last plan / result
        last_tool = working.get("last_tool_result", {}) or {}
        plan = (last_tool.get("data") or {}).get("plan", {}) or {}
        for maybe in [
            plan.get("project_path", ""),
            (working.get("last_plan") or {}).get("project_path", ""),
        ]:
            proj = self._maybe_existing_path(maybe)
            if proj:
                candidates.append((proj, Path(proj).name, 0.70, "last_plan"))

        # 6) newest remembered context with valid path
        for ctx in reversed(self._all_contexts()):
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            if proj:
                candidates.append(
                    (
                        proj,
                        str(ctx.get("name", "")).strip() or Path(proj).name,
                        0.45,
                        "recent_context",
                    )
                )
                break

        if not candidates:
            return {"project_path": "", "project_name": "", "source": "none"}

        best = sorted(candidates, key=lambda item: item[2], reverse=True)[0]
        return {"project_path": best[0], "project_name": best[1], "source": best[3]}

    def infer_intent(self, request_text: str) -> tuple[str, float, str]:
        q = self._norm(request_text)
        if not q:
            return "general", 0.15, "No request text."

        if any(
            x in q
            for x in [
                "last night",
                "yesterday",
                "this morning",
                "this afternoon",
                "earlier today",
                "what did i do",
                "what was i doing",
            ]
        ):
            return "activity_recall", 0.94, "Detected time-based activity recall."

        if any(
            x in q
            for x in [
                "what am i looking at",
                "what's on my screen",
                "what is on my screen",
                "summarize the tab",
                "summarize this tab",
                "summarize the screen",
                "summarize this screen",
                "what does this page do",
                "what does this file do",
            ]
        ):
            return (
                "screen_summary",
                0.96,
                "Detected current-screen summarization request.",
            )

        if any(
            x in q
            for x in [
                "search for",
                "look up",
                "find me",
                "movies playing near me",
                "what movies are playing near me",
                "google",
                "search the web",
                "search movies",
                "search for movies",
                "historical thing",
                "explain the ming dynasty",
                "explain ",
            ]
        ):
            return "knowledge_search", 0.90, "Detected web/knowledge search intent."

        if any(
            x in q
            for x in [
                "lock in",
                "focus",
                "deep work",
                "get to work",
                "get some work done",
                "lets get to work",
                "let's get to work",
            ]
        ):
            return "focus_session", 0.93, "Detected focus/work intent."

        if any(
            x in q for x in ["movie", "cinematic", "watch something", "watch a movie"]
        ):
            return "media_movie", 0.93, "Detected movie/cinematic request."

        if any(
            x in q
            for x in [
                "xbox",
                "youtube on",
                "netflix on",
                "spotify on",
                "pause the xbox",
                "resume the xbox",
                "resume youtube",
                "pause xbox",
            ]
        ):
            return "media_xbox", 0.95, "Detected Xbox/media request."

        if any(
            x in q
            for x in [
                "light",
                "lights",
                "calm",
                "cozy",
                "dim",
                "warmer",
                "cooler",
                "party mode",
                "night mode",
                "movie mode",
                "work mode",
            ]
        ):
            return "lighting", 0.90, "Detected lighting/mood request."

        if any(
            x in q
            for x in [
                "resume",
                "continue",
                "same as before",
                "restore",
                "open that project",
                "previous work",
                "switch to",
                "work on microschool",
                "work on prometheus",
            ]
        ):
            return "context_resume", 0.86, "Detected resume-context intent."

        if q.startswith(("open ", "launch ")):
            return "open_target", 0.78, "Detected open/launch request."

        return "general", 0.40, "General request."

    def build_recent_activity(self, query: str = "", limit: int = 12) -> dict[str, Any]:
        now = time.localtime()
        q = self._norm(query)
        events = []
        try:
            raw = self.episodes.tail(350)
        except Exception:
            raw = []
        for e in raw:
            if e.get("kind") not in {"tool_action", "tool_request"}:
                continue
            ts = str(e.get("ts", "")).strip()
            label = str(e.get("summary", "")).strip()
            if not ts or not label:
                continue
            if any(
                skip in label.lower()
                for skip in ["recent activity", "smart action", "learned behavior rule"]
            ):
                continue
            try:
                t = time.strptime(ts.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if self._match_time_window(q, t, now):
                events.append(
                    {
                        "ts": ts.replace("T", " "),
                        "summary": label,
                        "data": e.get("data", {}),
                    }
                )
        if not events:
            events = [
                {
                    "ts": str(e.get("ts", "")).replace("T", " "),
                    "summary": str(e.get("summary", "")),
                }
                for e in raw[-limit:]
                if str(e.get("summary", "")).strip()
            ]
        events = events[-limit:]
        summary = " | ".join(
            f"{item['ts'][11:16]} {item['summary']}" for item in events[-6:]
        )
        return {"items": events, "summary": summary}

    def _match_time_window(
        self, query: str, t: time.struct_time, now: time.struct_time
    ) -> bool:
        if not query:
            return True
        same_day = (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday)
        yesterday = (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday - 1)
        if "last night" in query:
            return yesterday and t.tm_hour >= 18
        if "yesterday" in query:
            return yesterday
        if "this morning" in query:
            return same_day and 5 <= t.tm_hour < 12
        if "this afternoon" in query:
            return same_day and 12 <= t.tm_hour < 18
        if "earlier today" in query or "today" in query:
            return same_day
        return True

    def _extract_trigger_phrase(self, text: str, fallback_intent: str) -> str:
        raw = text.strip()
        patterns = [
            r'next time when i say\s+["“]?([^"”,.!?]+)',
            r'when i say\s+["“]?([^"”,.!?]+)',
            r'if i say\s+["“]?([^"”,.!?]+)',
        ]
        lower = raw.lower()
        for pat in patterns:
            m = re.search(pat, lower)
            if m:
                phrase = m.group(1).strip(' "”.,!?')
                if phrase:
                    return phrase
        if fallback_intent == "focus_session":
            return "lock in"
        return fallback_intent.replace("_", " ")

    def _extract_plan_patch(
        self, text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        q = self._norm(text)
        add_steps: list[dict[str, Any]] = []
        remove_steps: list[dict[str, Any]] = []
        conditions: dict[str, Any] = {}

        if any(
            x in q for x in ["visual studio code", "vs code", "vscode", "code editor"]
        ):
            add_steps.append(
                {"action": "open_code_folder", "project_binding": "active_project"}
            )
        if "terminal" in q:
            add_steps.append(
                {"action": "open_terminal_here", "project_binding": "active_project"}
            )
        if "spotify" in q and any(
            x in q
            for x in [
                "not spotify",
                "never include spotify",
                "dont open spotify",
                "don't open spotify",
            ]
        ):
            remove_steps.append({"action": "open_app", "app": "spotify"})
        if "spotify" in q and any(
            x in q for x in ["and spotify", "open spotify too", "also open spotify"]
        ):
            add_steps.append({"action": "open_app", "app": "spotify"})
        if "google" in q and any(
            x in q for x in ["not google", "dont open google", "don't open google"]
        ):
            remove_steps.append({"action": "open_url_key", "url_key": "google"})
            remove_steps.append({"action": "open_app", "app": "chrome"})
        if add_steps and any("project_binding" in step for step in add_steps):
            conditions["active_project_preferred"] = True
        return add_steps, remove_steps, conditions

    def learn_from_correction(
        self, request_text: str, desktop_state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        working = self._read_working()
        fallback_intent = (
            self._norm((working.get("last_plan") or {}).get("intent", ""))
            or "focus_session"
        )
        trigger_phrase = self._extract_trigger_phrase(request_text, fallback_intent)
        inferred_from_trigger, _, _ = self.infer_intent(trigger_phrase)
        target_intent = (
            inferred_from_trigger
            if inferred_from_trigger != "general"
            else fallback_intent
        )
        add_steps, remove_steps, conditions = self._extract_plan_patch(request_text)
        if not add_steps and not remove_steps:
            return {
                "ok": False,
                "message": "I understood that as a preference note, but I could not compile any executable behavior from it.",
            }

        rule_id = f"{self._slug(target_intent)}-{self._slug(trigger_phrase)}"
        rule = {
            "rule_id": rule_id,
            "trigger_phrase": trigger_phrase,
            "target_intent": target_intent,
            "add_steps": add_steps,
            "remove_steps": remove_steps,
            "reason": "Compiled a reusable behavior rule from a user correction.",
            "confidence": 0.98,
            "trigger_aliases": [],
            "conditions": conditions,
            "source_text": request_text,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.semantic.set_fact(
            f"behavior_rule::{rule_id}",
            rule,
            confidence=0.98,
            source="behavior_learning",
            tags=["behavior_rule", "prometheus_v4"],
        )
        try:
            self.working.set_preference_edit(rule)
        except Exception:
            self.working.write({"last_preference_edit": rule})
        return {
            "ok": True,
            "rule": rule,
            "message": f"Learned a reusable behavior rule for '{trigger_phrase}'.",
        }

    def _matching_rules(self, request_text: str, intent: str) -> list[dict[str, Any]]:
        rules = []
        q = self._norm(request_text)
        target = self._norm(intent.replace("_", " "))
        for fact in self.semantic.get_facts_by_tag("behavior_rule", min_confidence=0.5):
            value = fact.get("value") or {}
            rule_target = self._norm(
                str(value.get("target_intent", "")).replace("_", " ")
            )
            trigger = self._norm(value.get("trigger_phrase", ""))
            aliases = [self._norm(x) for x in value.get("trigger_aliases", [])]
            if rule_target and rule_target != target:
                continue
            if trigger and (
                trigger in q or q in trigger or any(a and a in q for a in aliases)
            ):
                rules.append(value)
            elif target and rule_target == target:
                rules.append(value)
        return sorted(
            rules, key=lambda r: float(r.get("confidence", 0.0)), reverse=True
        )

    def _resolve_binding_step(
        self,
        step: dict[str, Any],
        desktop_state: dict[str, Any] | None = None,
        request_text: str = "",
    ) -> dict[str, Any] | None:
        step = dict(step)
        binding = str(step.pop("project_binding", "")).strip()
        if binding == "active_project":
            active = self.resolve_active_project(
                desktop_state, request_text=request_text
            )
            p = self._maybe_existing_path(active.get("project_path", ""))
            if not p:
                return None
            step["project_path"] = p
        return step

    def apply_rules(
        self,
        plan: dict[str, Any],
        request_text: str,
        desktop_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        steps = [dict(x) for x in plan.get("steps", [])]
        rules = self._matching_rules(request_text, str(plan.get("intent", "")))
        for rule in rules:
            remove_specs = rule.get("remove_steps", []) or []
            if remove_specs:
                kept = []
                for step in steps:
                    should_remove = False
                    for spec in remove_specs:
                        if all(step.get(k) == v for k, v in spec.items()):
                            should_remove = True
                            break
                    if not should_remove:
                        kept.append(step)
                steps = kept
            for raw in rule.get("add_steps", []) or []:
                resolved = self._resolve_binding_step(raw, desktop_state, request_text)
                if resolved and resolved not in steps:
                    steps.append(resolved)
        plan = dict(plan)
        plan["steps"] = steps
        if rules:
            plan["applied_rules"] = [r.get("rule_id") for r in rules]
        return plan

    def _resolve_target_project_path(
        self, request_text: str, desktop_state: dict[str, Any] | None = None
    ) -> str:
        q = self._norm(request_text)
        for ctx in self._all_contexts():
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            if not proj:
                continue
            if self._norm(ctx.get("name", "")) in q or self._norm(Path(proj).name) in q:
                return proj
        active = self.resolve_active_project(desktop_state, request_text=request_text)
        return self._maybe_existing_path(active.get("project_path", ""))

    def _match_named_context(self, request_text: str) -> dict[str, Any] | None:
        q = self._norm(request_text)
        if not q:
            return None
        for ctx in self._all_contexts():
            name = self._norm(ctx.get("name", ""))
            proj = self._maybe_existing_path(ctx.get("project_path", ""))
            proj_name = self._norm(Path(proj).name) if proj else ""
            if (name and name in q) or (proj_name and proj_name in q):
                return ctx
        return None

    def _workspace_steps_for_context(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        if not ctx:
            return []
        name = str(ctx.get("name", "")).strip()
        if name:
            return [{"action": "resume_last_context", "context_name": name}]
        return []

    def _lighting_script_for_query(self, q: str) -> str:
        if "off" in q:
            return "jarvis_lights_off"
        if "on" in q:
            return "jarvis_lights_on"
        if "red" in q:
            return "jarvis_lights_red"
        if "blue" in q:
            return "jarvis_lights_blue"
        if "green" in q:
            return "jarvis_lights_green"
        if "purple" in q:
            return "jarvis_lights_purple"
        if "brighten" in q or "brighter" in q:
            return "jarvis_lights_brighten"
        if "dim" in q or "dimmer" in q:
            return "jarvis_lights_dim"
        if "night mode" in q:
            return "jarvis_night_mode"
        if "party mode" in q:
            return "jarvis_party_mode"
        if "movie mode" in q or "cozy" in q or "calm" in q:
            return "jarvis_movie_mode"
        if "work mode" in q or "focus" in q:
            return "jarvis_work_mode"
        if "natural" in q or "default" in q:
            return "jarvis_lights_default"
        return "jarvis_lights_on"

    def _xbox_script_for_query(self, q: str) -> str:
        if "pause" in q:
            return "jarvis_xbox_pause"
        if "resume" in q or "play" in q:
            if "youtube" in q:
                return "jarvis_xbox_youtube"
            if "netflix" in q:
                return "jarvis_xbox_netflix"
            if "spotify" in q:
                return "jarvis_xbox_spotify"
            return "jarvis_xbox_resume"
        if "off" in q:
            return "jarvis_xbox_off"
        if "youtube" in q:
            return "jarvis_xbox_youtube"
        if "netflix" in q:
            return "jarvis_xbox_netflix"
        if "spotify" in q:
            return "jarvis_xbox_spotify"
        if "volume up" in q or "turn it up" in q:
            return "jarvis_xbox_volume_up"
        if "volume down" in q or "turn it down" in q:
            return "jarvis_xbox_volume_down"
        return "jarvis_xbox_on"

    def build_plan(
        self,
        request_text: str,
        desktop_state: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        intent, confidence, reason = self.infer_intent(request_text)
        q = self._norm(request_text)
        steps: list[dict[str, Any]] = []
        plan: dict[str, Any] = {
            "request_text": request_text,
            "intent": intent,
            "confidence": confidence,
            "reason": reason,
            "steps": steps,
        }

        if intent == "activity_recall":
            activity = self.build_recent_activity(request_text)
            plan["summary"] = activity["summary"]
            plan["items"] = activity["items"]
            return plan

        if intent == "screen_summary":
            steps.append({"action": "summarize_screen", "request_text": request_text})
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "knowledge_search":
            query = request_text.strip()
            if q.startswith("explain "):
                query = request_text.strip()
            steps.append({"action": "web_search", "query": query})
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "media_movie":
            if "youtube" in q:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_watch_youtube"}
                )
            elif "netflix" in q:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_watch_netflix"}
                )
            else:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_movie_mode"}
                )
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "media_xbox":
            steps.append(
                {
                    "action": "run_ha_script",
                    "script_name": self._xbox_script_for_query(q),
                }
            )
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "lighting":
            steps.append(
                {
                    "action": "run_ha_script",
                    "script_name": self._lighting_script_for_query(q),
                }
            )
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "focus_session":
            ctx = self._match_named_context(request_text)
            if ctx:
                steps.extend(self._workspace_steps_for_context(ctx))
            else:
                active = self.resolve_active_project(
                    desktop_state, request_text=request_text
                )
                if active.get("project_path"):
                    steps.append(
                        {
                            "action": "resume_last_context",
                            "query": active.get("project_name") or request_text,
                        }
                    )
                else:
                    last = None
                    try:
                        last = self.memory.get_last_context()  # type: ignore[attr-defined]
                    except Exception:
                        last = None
                    if last:
                        steps.extend(self._workspace_steps_for_context(last))
                    else:
                        steps.append({"action": "mode_lock_in"})
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "context_resume":
            ctx = self._match_named_context(request_text)
            if ctx:
                steps.extend(self._workspace_steps_for_context(ctx))
            else:
                steps.append({"action": "resume_last_context", "query": request_text})
            return self.apply_rules(plan, request_text, desktop_state)

        if intent == "open_target":
            ctx = self._match_named_context(request_text)
            if ctx and any(
                x in q
                for x in [
                    "project",
                    "workspace",
                    "jarvis",
                    "prometheus",
                    "microschool",
                    "tileworld",
                    "lumen",
                    "truth",
                    "daemon",
                ]
            ):
                steps.extend(self._workspace_steps_for_context(ctx))
                return self.apply_rules(plan, request_text, desktop_state)

            if "terminal" in q:
                proj = self._resolve_target_project_path(request_text, desktop_state)
                if proj:
                    steps.append({"action": "open_terminal_here", "project_path": proj})
                else:
                    steps.append({"action": "open_app", "app": "terminal"})
            elif any(
                x in q for x in ["visual studio code", "vs code", "vscode", "code"]
            ):
                proj = self._resolve_target_project_path(request_text, desktop_state)
                if proj:
                    steps.append({"action": "open_code_folder", "project_path": proj})
                else:
                    steps.append({"action": "open_app", "app": "code"})
            elif "spotify" in q and "xbox" in q:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_xbox_spotify"}
                )
            elif "spotify" in q:
                steps.append({"action": "open_app", "app": "spotify"})
            elif "youtube" in q and "xbox" in q:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_xbox_youtube"}
                )
            elif "netflix" in q and "xbox" in q:
                steps.append(
                    {"action": "run_ha_script", "script_name": "jarvis_xbox_netflix"}
                )
            elif "google" in q or "browser" in q or "chrome" in q:
                steps.append({"action": "open_app", "app": "chrome"})
            else:
                steps.append({"action": "open_app", "app": "terminal"})
            return self.apply_rules(plan, request_text, desktop_state)

        if any(x in q for x in ["what apps", "what windows", "what do i have open"]):
            steps.append({"action": "list_windows"})
        elif any(x in q for x in ["what am i focused on", "what app am i in"]):
            steps.append({"action": "get_active_window"})
        elif q:
            steps.append({"action": "web_search", "query": request_text})

        return self.apply_rules(plan, request_text, desktop_state)
