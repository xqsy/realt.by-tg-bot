from __future__ import annotations

from app.models import Listing, UserPreferences

HIDDEN_ATTRIBUTE_SECTIONS = {"Местоположение"}
HIDDEN_ATTRIBUTE_KEYS = {"furniture", "мебель"}


def _normalize_attribute_key(value: object) -> str:
    return str(value).strip().lower()


def format_preferences(prefs: UserPreferences, city_label: str) -> str:
    min_price = str(prefs.min_price) if prefs.min_price is not None else "не задана"
    max_price = str(prefs.max_price) if prefs.max_price is not None else "не задана"
    rooms = str(prefs.rooms) if prefs.rooms is not None else "любое"
    return (
        "Текущие параметры поиска\n"
        f"Город: {city_label}\n"
        f"Цена от: {min_price}\n"
        f"Цена до: {max_price}\n"
        f"Комнаты: {rooms}\n\n"
        "Можно также написать запрос в свободной форме для ИИ-поиска, например: двушка в Минске до 1200 рядом с метро"
    )


def format_listing_short(index: int, listing: Listing) -> str:
    pieces = [f"Объявление {index}", f"Цена: {listing.price_label}"]
    details: list[str] = []
    if listing.rooms is not None:
        details.append(f"Комнаты: {listing.rooms}")
    if listing.area_m2 is not None:
        details.append(f"Площадь: {listing.area_m2:g} м²")
    if listing.floor is not None and listing.floors_total is not None:
        details.append(f"Этаж: {listing.floor} из {listing.floors_total}")
    if details:
        pieces.extend(details)
    if listing.address:
        pieces.append(f"Адрес: {listing.address}")
    if listing.contact_name:
        pieces.append(f"Контакт: {listing.contact_name}")
    pieces.append(listing.url)
    return "\n".join(pieces)


def format_listing_full(listing: Listing) -> str:
    lines = [f"Цена: {listing.price_label}"]
    if listing.address:
        lines.append(f"Адрес: {listing.address}")
    overview: list[str] = []
    if listing.rooms is not None:
        overview.append(f"Комнаты: {listing.rooms}")
    if listing.area_m2 is not None:
        overview.append(f"Площадь: {listing.area_m2:g} м²")
    if listing.floor is not None and listing.floors_total is not None:
        overview.append(f"Этаж: {listing.floor} из {listing.floors_total}")
    if overview:
        lines.append("Характеристики:")
        for item in overview:
            lines.append(f"- {item}")
    if listing.district:
        lines.append(f"Район: {listing.district}")
    if listing.metro:
        lines.append(f"Метро: {listing.metro}")
    if listing.contact_name:
        lines.append(f"Контакт: {listing.contact_name}")
    if listing.phone_numbers:
        lines.append("Телефоны: " + ", ".join(listing.phone_numbers))
    if listing.description:
        lines.append("")
        lines.append("Описание:")
        lines.append(listing.description[:1800])
    if listing.attributes:
        for key, value in listing.attributes.items():
            if key in HIDDEN_ATTRIBUTE_SECTIONS:
                continue
            normalized_key = _normalize_attribute_key(key)
            if normalized_key in HIDDEN_ATTRIBUTE_KEYS:
                continue
            lines.append("")
            lines.append(f"{key}:")
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if _normalize_attribute_key(nested_key) in HIDDEN_ATTRIBUTE_KEYS:
                        continue
                    lines.append(f"- {nested_key}: {nested_value}")
            elif isinstance(value, list):
                for item in value:
                    lines.append(f"- {item}")
            else:
                lines.append(f"- {value}")
    lines.append("")
    lines.append("Ссылка:")
    lines.append(listing.url)
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks
