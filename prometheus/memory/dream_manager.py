from __future__ import annotations

from collections import Counter

from prometheus.memory.episodic_memory import EpisodicMemory
from prometheus.memory.procedural_memory import ProceduralMemory
from prometheus.memory.semantic_memory import SemanticMemory


class DreamManager:
    def __init__(self) -> None:
        self.episodes = EpisodicMemory()
        self.semantic = SemanticMemory()
        self.procedural = ProceduralMemory()

    def run_once(self) -> dict[str, int]:
        events = self.episodes.tail(300)

        fact_updates = 0
        routine_updates = 0

        # Promote repeated stable truths
        summaries = [e.get("summary", "") for e in events]
        joined = " || ".join(summaries).lower()

        if "cloud-hosted" in joined or "cloud hosted" in joined:
            self.semantic.set_fact(
                "microschool_hosting",
                "cloud_hosted",
                confidence=0.95,
                source="dream_pass",
                tags=["microschool", "hosting"],
            )
            fact_updates += 1

        if "movie_mode_full" in joined or "netflix_on_xbox" in joined:
            self.semantic.set_fact(
                "xbox_movie_control",
                "home_assistant",
                confidence=0.95,
                source="dream_pass",
                tags=["xbox", "home_assistant", "media"],
            )
            fact_updates += 1

        # Promote repeated flows into procedures
        action_counter = Counter()
        for e in events:
            kind = e.get("kind", "")
            if kind == "tool_action":
                action = e.get("data", {}).get("action", "")
                if action:
                    action_counter[action] += 1

        if action_counter["run_ha_script"] >= 3:
            self.procedural.save_routine(
                "movie_mode_full",
                description="Turn on movie mode through Home Assistant.",
                triggers=["turn on a movie", "movie mode", "start movie mode"],
                steps=[{"action": "run_ha_script", "script_name": "movie_mode_full"}],
                tags=["xbox", "movie", "lights"],
            )
            routine_updates += 1

        if action_counter["list_windows"] >= 3:
            self.procedural.save_routine(
                "desktop_status_check",
                description="Check current open apps and focused window.",
                triggers=["what apps do i have open", "what am i focused on"],
                steps=[{"action": "desktop_state"}],
                tags=["desktop", "awareness"],
            )
            routine_updates += 1

        return {
            "fact_updates": fact_updates,
            "routine_updates": routine_updates,
        }
