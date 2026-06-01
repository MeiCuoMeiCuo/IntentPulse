import json
import os
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
PLACEHOLDER_VALUES = {
    "",
    "YOUR_OPENROUTER_KEY",
    "YOUR_OPENROUTER_API_KEY",
    "sk-...",
}


def _is_real_value(value: Optional[str]) -> bool:
    return bool(value and value.strip() not in PLACEHOLDER_VALUES)


def get_openrouter_api_key(config_path: Optional[str] = None) -> str:
    env_key = os.getenv(OPENROUTER_API_KEY_ENV, "").strip()
    if _is_real_value(env_key):
        return env_key

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        api_key = str(data.get("openrouter_api_key", "")).strip()
        if _is_real_value(api_key):
            return api_key

    raise ValueError(
        "Missing OpenRouter API key. Set OPENROUTER_API_KEY or copy "
        "config.example.json to config.json."
    )
