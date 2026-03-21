from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UserPreferences:
    user_id: int
    city_key: str = "minsk"
    min_price: int | None = None
    max_price: int | None = None
    rooms: int | None = None


@dataclass(slots=True)
class Listing:
    listing_id: str
    url: str
    title: str
    city_label: str
    price_byn: int | None = None
    price_usd: int | None = None
    rooms: int | None = None
    area_m2: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    address: str | None = None
    district: str | None = None
    metro: str | None = None
    description: str | None = None
    phone_numbers: list[str] = field(default_factory=list)
    contact_name: str | None = None
    published_at: str | None = None
    photo_urls: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def price_label(self) -> str:
        parts: list[str] = []
        if self.price_byn is not None:
            parts.append(f"{self.price_byn} р./мес.")
        if self.price_usd is not None:
            parts.append(f"≈ {self.price_usd} $/мес.")
        return " | ".join(parts) or "Цена не указана"


@dataclass(slots=True)
class SearchResult:
    items: list[Listing]
    source_url: str
