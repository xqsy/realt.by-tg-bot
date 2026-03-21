from __future__ import annotations

from app.models import Listing, UserPreferences


def format_preferences(prefs: UserPreferences, city_label: str) -> str:
    min_price = str(prefs.min_price) if prefs.min_price is not None else "не задана"
    max_price = str(prefs.max_price) if prefs.max_price is not None else "не задана"
    rooms = str(prefs.rooms) if prefs.rooms is not None else "любое"
    return (
        f"Город: {city_label}\n"
        f"Мин. цена: {min_price}\n"
        f"Макс. цена: {max_price}\n"
        f"Комнаты: {rooms}"
    )


def format_listing_short(index: int, listing: Listing) -> str:
    pieces = [f"{index}. {listing.title}", listing.price_label]
    details: list[str] = []
    if listing.rooms is not None:
        details.append(f"{listing.rooms} комн.")
    if listing.area_m2 is not None:
        details.append(f"{listing.area_m2:g} м²")
    if listing.floor is not None and listing.floors_total is not None:
        details.append(f"{listing.floor}/{listing.floors_total} этаж")
    if details:
        pieces.append(" | ".join(details))
    if listing.address:
        pieces.append(listing.address)
    pieces.append(listing.url)
    return "\n".join(pieces)


def format_listing_full(listing: Listing) -> str:
    lines = [listing.title, listing.price_label]
    if listing.address:
        lines.append(f"Адрес: {listing.address}")
    if listing.rooms is not None:
        lines.append(f"Комнаты: {listing.rooms}")
    if listing.area_m2 is not None:
        lines.append(f"Площадь: {listing.area_m2:g} м²")
    if listing.floor is not None and listing.floors_total is not None:
        lines.append(f"Этаж: {listing.floor}/{listing.floors_total}")
    if listing.district:
        lines.append(f"Район: {listing.district}")
    if listing.metro:
        lines.append(f"Метро: {listing.metro}")
    if listing.contact_name:
        lines.append(f"Контакт: {listing.contact_name}")
    if listing.phone_numbers:
        lines.append("Телефоны: " + ", ".join(listing.phone_numbers))
    if listing.published_at:
        lines.append(f"Дата: {listing.published_at}")
    if listing.description:
        lines.append("")
        lines.append(listing.description[:1800])
    if listing.attributes:
        for key, value in listing.attributes.items():
            lines.append("")
            lines.append(f"{key}:")
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    lines.append(f"- {nested_key}: {nested_value}")
            elif isinstance(value, list):
                for item in value:
                    lines.append(f"- {item}")
            else:
                lines.append(f"- {value}")
    lines.append("")
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
