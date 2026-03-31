from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CITY_URLS = {
    "minsk": ("Минск", "https://realt.by/rent/flat-for-long/"),
    "brest": ("Брест", "https://realt.by/brest-region/rent/flat-for-long/"),
    "mogilev": ("Могилев", "https://realt.by/mogilev-region/rent/flat-for-long/"),
    "gomel": ("Гомель", "https://realt.by/gomel-region/rent/flat-for-long/"),
    "grodno": ("Гродно", "https://realt.by/grodno-region/rent/flat-for-long/"),
    "vitebsk": ("Витебск", "https://realt.by/vitebsk-region/rent/flat-for-long/"),
}

CITY_UUIDS: dict[str, str] = {
    "minsk":   "4cb07174-7b00-11eb-8943-0cc47adabd66",
    "brest":   "4c8f8db2-7b00-11eb-8943-0cc47adabd66",
    "gomel":   "4c95d414-7b00-11eb-8943-0cc47adabd66",
    "grodno":  "4c97eac6-7b00-11eb-8943-0cc47adabd66",
    "vitebsk": "4c9236d8-7b00-11eb-8943-0cc47adabd66",
    "mogilev": "4cb0e950-7b00-11eb-8943-0cc47adabd66",
}


@dataclass(slots=True)
class Settings:
    bot_token: str
    request_timeout: int
    data_dir: Path
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    ai_enable_reasoning: bool


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "data")).resolve()
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        data_dir=data_dir,
        ai_api_key=os.getenv("AI_API_KEY", ""),
        ai_base_url=os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1"),
        ai_model=os.getenv("AI_MODEL", ""),
        ai_enable_reasoning=os.getenv("AI_ENABLE_REASONING", "false").strip().lower() in {"1", "true", "yes", "on"},
    )
