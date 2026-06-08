from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

HOME = Path.home()
BASE_DIR = HOME / ".jarvis"
LOG_DIR = BASE_DIR / "logs"
AUDIO_DIR = BASE_DIR / "audio"
CACHE_DIR = AUDIO_DIR / "cache"
WAKEWORD_DIR = BASE_DIR / "wakewords"
CONFIG_PATH = BASE_DIR / "config.json"
VISUAL_STATE_PATH = BASE_DIR / "visual_state.json"

for path in (BASE_DIR, LOG_DIR, AUDIO_DIR, CACHE_DIR, WAKEWORD_DIR):
    path.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG: dict[str, Any] = {
    "openai_api_key": "",
    "realtime_model": "gpt-realtime",
    "voice": "alloy",
    "sample_rate_out": 24000,
    "sample_rate_in": 16000,
    "mic_device": None,
    "speaker_blocksize": 2048,
    # Push-to-talk
    "ptt_hold_seconds": 0.25,
    "ptt_key": "esc",
    # Wake word
    "enable_wake_word": True,
    "wake_word_engine": "porcupine",
    "wake_word_access_key": "",
    "wake_word_keyword_path": str(WAKEWORD_DIR / "jarvis.ppn"),
    "wake_word_sensitivity": 0.65,
    "wake_word_cooldown_seconds": 1.25,
    "wake_word_min_listen_seconds": 0.90,
    "wake_word_end_silence_seconds": 1.10,
    "wake_word_energy_threshold": 550.0,
    # Turn behavior
    "max_turn_seconds": 12.0,
    "screenshot_dir": str(HOME / "Pictures" / "Screenshots"),
    "projector_on_script": str(HOME / ".jarvis" / "projector_on.sh"),
    "projector_off_script": str(HOME / ".jarvis" / "projector_off.sh"),
    "apps": {
        "chrome": "google-chrome",
        "browser": "google-chrome",
        "code": "code",
        "vscode": "code",
        "terminal": "konsole",
        "files": "dolphin",
        "spotify": "spotify",
        "discord": "discord",
        "obsidian": "obsidian",
    },
    "urls": {
        "youtube": "https://youtube.com",
        "gmail": "https://mail.google.com",
        "calendar": "https://calendar.google.com",
        "chatgpt": "https://chatgpt.com",
        "google": "https://www.google.com",
    },
    "modes": {
        "lock_in": ["google-chrome", "code", "konsole"],
        "movie": ["google-chrome"],
    },
    "layout_script": str(HOME / ".jarvis" / "layout_workspace.sh"),
    "project_search_roots": [
        str(HOME / "Desktop"),
        str(HOME / "Documents"),
        str(HOME / "Projects"),
    ],
    "camera_sources": {
        "right_camera": "/dev/video0",
        "left_camera": "",
        "topdown_camera": "",
    },
    "camera_roles": {
        "right_camera": "environment",
        "left_camera": "whiteboard",
        "topdown_camera": "desk",
    },
    "gesture_control_enabled": False,
    "vision_enabled": True,
    "vault_path": "",
    "ha_light_entity": "",      # e.g. "light.rgb_strip" — leave empty to skip light verification
    "workspace_poll_interval": 5.0,
    "ollama_model": "mistral",
    "ollama_url": "http://localhost:11434",
    "projects": {},
    "routines": {
        "movie_mode_full": {
            "description": "Turn on movie mode through Home Assistant.",
            "steps": [{"action": "run_ha_script", "script_name": "movie_mode_full"}],
        },
        "netflix_on_xbox": {
            "description": "Launch Netflix on Xbox through Home Assistant.",
            "steps": [{"action": "run_ha_script", "script_name": "netflix_on_xbox"}],
        },
    },
}


def _deep_copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _read_runtime_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{CONFIG_PATH} must contain a JSON object")

    return data


def load_config() -> dict[str, Any]:
    config = _deep_copy_dict(DEFAULT_CONFIG)
    runtime = _read_runtime_config()

    for key, value in runtime.items():
        if key in {"apps", "urls", "modes", "projects", "routines"} and isinstance(
            value, dict
        ):
            base = config.get(key, {})
            if isinstance(base, dict):
                config[key] = {**base, **value}
            else:
                config[key] = value
        else:
            config[key] = value

    env_key = os.getenv("OPENAI_API_KEY", "").strip()
    if env_key:
        config["openai_api_key"] = env_key

    env_ww_key = os.getenv("PORCUPINE_ACCESS_KEY", "").strip()
    if env_ww_key:
        config["wake_word_access_key"] = env_ww_key

    return config


CONFIG = load_config()
