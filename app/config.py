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


@dataclass(slots=True)
class Settings:
    bot_token: str
    request_timeout: int
    max_pages_per_city: int
    data_dir: Path


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "data")).resolve()
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        max_pages_per_city=int(os.getenv("MAX_PAGES_PER_CITY", "3")),
        data_dir=data_dir,
    )
