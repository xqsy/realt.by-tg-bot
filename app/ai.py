from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from app.config import CITY_URLS, Settings
from app.models import Listing, UserPreferences

ROOM_ALIASES = {
    "студ": 1,
    "одн": 1,
    "1к": 1,
    "1-ком": 1,
    "2к": 2,
    "2-ком": 2,
    "двуш": 2,
    "двух": 2,
    "3к": 3,
    "3-ком": 3,
    "треш": 3,
    "трех": 3,
    "4к": 4,
    "4-ком": 4,
    "четырех": 4,
}
STOPWORDS = {
    "ищу",
    "нужна",
    "нужно",
    "нужен",
    "квартира",
    "квартиру",
    "аренда",
    "снять",
    "долгосрочно",
    "длительно",
    "долгий",
    "срок",
    "в",
    "во",
    "на",
    "по",
    "до",
    "от",
    "и",
    "или",
    "не",
    "но",
    "около",
    "рядом",
    "желательно",
    "желателен",
    "хочу",
    "мне",
    "для",
    "без",
    "чтобы",
}


@dataclass(slots=True)
class QueryAnalysis:
    original_query: str
    city_key: str | None = None
    min_price: int | None = None
    max_price: int | None = None
    rooms: int | None = None
    features: list[str] = field(default_factory=list)
    summary: str = ""
    source: str = "heuristic"

    def has_updates(self) -> bool:
        return any(
            value is not None for value in (self.city_key, self.min_price, self.max_price, self.rooms)
        ) or bool(self.features)


class HousingQueryAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze(self, query: str, current_prefs: UserPreferences) -> QueryAnalysis:
        heuristic = self._heuristic_parse(query, current_prefs)
        if not self._settings.ai_api_key or not self._settings.ai_model:
            return heuristic
        try:
            remote = await self._remote_parse(query, current_prefs)
        except Exception as exc:
            logging.warning("AI query analysis fallback triggered: %s", exc)
            return heuristic
        return self._merge_with_heuristic(remote, heuristic)

    def rank_listings(self, listings: list[Listing], analysis: QueryAnalysis, prefs: UserPreferences) -> list[Listing]:
        scored = sorted(
            listings,
            key=lambda item: self._score_listing(item, analysis, prefs),
            reverse=True,
        )
        return scored

    async def _remote_parse(self, query: str, current_prefs: UserPreferences) -> QueryAnalysis:
        city_options = ", ".join(f"{key}: {label}" for key, (label, _) in CITY_URLS.items())
        prompt = (
            "Ты анализируешь запрос пользователя для подбора квартиры в долгосрочную аренду. "
            "Верни только JSON с полями city_key, min_price, max_price, rooms, features, summary. "
            "city_key может быть только одним из: "
            f"{city_options}. "
            "Если значение неизвестно, верни null. features должен быть массивом коротких строк. "
            "summary должен быть кратким русским описанием распознанных критериев."
        )
        user_payload = {
            "query": query,
            "current_preferences": {
                "city_key": current_prefs.city_key,
                "min_price": current_prefs.min_price,
                "max_price": current_prefs.max_price,
                "rooms": current_prefs.rooms,
            },
        }
        request_payload = {
            "model": self._settings.ai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        if self._settings.ai_enable_reasoning and "openrouter.ai" in self._settings.ai_base_url:
            request_payload["reasoning"] = {"enabled": True}
        async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
            response = await client.post(
                f"{self._settings.ai_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com",
                    "X-Title": "realt-bot",
                },
                json=request_payload,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"AI provider error {response.status_code}: {response.text[:500]}")
            response.raise_for_status()
        payload = response.json()
        content = str(payload["choices"][0]["message"].get("content") or "").strip()
        parsed = self._parse_json_response(content)
        city_key = parsed.get("city_key")
        if city_key not in CITY_URLS:
            city_key = None
        features = parsed.get("features")
        if not isinstance(features, list):
            features = []
        return QueryAnalysis(
            original_query=query,
            city_key=city_key,
            min_price=self._as_int(parsed.get("min_price")),
            max_price=self._as_int(parsed.get("max_price")),
            rooms=self._as_int(parsed.get("rooms")),
            features=[str(item).strip().lower() for item in features if str(item).strip()],
            summary=str(parsed.get("summary") or "").strip(),
            source="api",
        )

    def _heuristic_parse(self, query: str, current_prefs: UserPreferences) -> QueryAnalysis:
        normalized = query.lower().replace("ё", "е")
        city_key = self._extract_city_key(normalized)
        min_price, max_price = self._extract_price_range(normalized)
        rooms = self._extract_rooms(normalized)
        features = self._extract_features(normalized)
        parts: list[str] = []
        effective_city_key = city_key or current_prefs.city_key
        parts.append(f"город: {CITY_URLS[effective_city_key][0]}")
        if min_price is not None or max_price is not None:
            left = str(min_price) if min_price is not None else "без минимума"
            right = str(max_price) if max_price is not None else "без максимума"
            parts.append(f"цена: {left} - {right} BYN")
        if rooms is not None:
            parts.append(f"комнаты: {rooms}")
        if features:
            parts.append("пожелания: " + ", ".join(features[:5]))
        return QueryAnalysis(
            original_query=query,
            city_key=city_key,
            min_price=min_price,
            max_price=max_price,
            rooms=rooms,
            features=features,
            summary="; ".join(parts),
            source="heuristic",
        )

    def _merge_with_heuristic(self, remote: QueryAnalysis, heuristic: QueryAnalysis) -> QueryAnalysis:
        return QueryAnalysis(
            original_query=remote.original_query,
            city_key=remote.city_key or heuristic.city_key,
            min_price=remote.min_price if remote.min_price is not None else heuristic.min_price,
            max_price=remote.max_price if remote.max_price is not None else heuristic.max_price,
            rooms=remote.rooms if remote.rooms is not None else heuristic.rooms,
            features=remote.features or heuristic.features,
            summary=remote.summary or heuristic.summary,
            source=remote.source,
        )

    def _extract_city_key(self, normalized_query: str) -> str | None:
        for city_key, (label, _) in CITY_URLS.items():
            city_name = label.lower().replace("ё", "е")
            if city_name in normalized_query:
                return city_key
        return None

    def _extract_price_range(self, normalized_query: str) -> tuple[int | None, int | None]:
        min_price = None
        max_price = None
        up_to_match = re.search(r"(?:до|не дороже|макс(?:имум)?)[^\d]{0,12}(\d{2,5})", normalized_query)
        if up_to_match:
            max_price = int(up_to_match.group(1))
        from_match = re.search(r"(?:от|не дешевле|мин(?:имум)?)[^\d]{0,12}(\d{2,5})", normalized_query)
        if from_match:
            min_price = int(from_match.group(1))
        between_match = re.search(r"(\d{2,5})\s*[-–]\s*(\d{2,5})", normalized_query)
        if between_match:
            left = int(between_match.group(1))
            right = int(between_match.group(2))
            min_price = min_price or min(left, right)
            max_price = max_price or max(left, right)
        if min_price is None and max_price is None:
            standalone = [int(value) for value in re.findall(r"\b(\d{3,5})\b", normalized_query)]
            plausible = [value for value in standalone if 100 <= value <= 10000]
            if len(plausible) == 1:
                max_price = plausible[0]
        return min_price, max_price

    def _extract_rooms(self, normalized_query: str) -> int | None:
        explicit = re.search(r"\b([1-4])\s*[- ]?(?:комнат|комн|к)\b", normalized_query)
        if explicit:
            return int(explicit.group(1))
        for alias, rooms in ROOM_ALIASES.items():
            if alias in normalized_query:
                return rooms
        return None

    def _extract_features(self, normalized_query: str) -> list[str]:
        features: list[str] = []
        for phrase in ["рядом с метро", "с метро", "центр", "без мебели", "с мебелью", "можно с животными", "с детьми"]:
            if phrase in normalized_query:
                features.append(phrase)
        tokens = re.findall(r"[а-яa-z0-9]{3,}", normalized_query)
        for token in tokens:
            if token in STOPWORDS:
                continue
            if token in ROOM_ALIASES:
                continue
            if token.isdigit():
                continue
            if any(token == label.lower().replace("ё", "е") for label, _ in CITY_URLS.values()):
                continue
            if token not in features:
                features.append(token)
        return features[:8]

    def _score_listing(self, listing: Listing, analysis: QueryAnalysis, prefs: UserPreferences) -> float:
        score = 0.0
        target_rooms = analysis.rooms if analysis.rooms is not None else prefs.rooms
        if target_rooms is not None:
            if listing.rooms == target_rooms:
                score += 3.0
            elif listing.rooms is not None:
                score -= abs(listing.rooms - target_rooms) * 1.5
        target_max_price = analysis.max_price if analysis.max_price is not None else prefs.max_price
        target_min_price = analysis.min_price if analysis.min_price is not None else prefs.min_price
        if listing.price_byn is not None:
            if target_max_price is not None:
                if listing.price_byn <= target_max_price:
                    score += max(0.0, 3.0 - (target_max_price - listing.price_byn) / 400)
                else:
                    score -= 4.0 + (listing.price_byn - target_max_price) / 250
            if target_min_price is not None:
                if listing.price_byn >= target_min_price:
                    score += 1.0
                else:
                    score -= 1.5
        haystack = " ".join(
            part.lower()
            for part in [
                listing.title,
                listing.address or "",
                listing.district or "",
                listing.metro or "",
                listing.description or "",
                " ".join(str(value) for value in listing.attributes.values()),
            ]
        )
        for feature in analysis.features:
            if feature and feature in haystack:
                score += 2.0
        if listing.metro and any("метро" in feature for feature in analysis.features):
            score += 1.0
        if listing.area_m2 is not None:
            score += min(listing.area_m2 / 100, 1.5)
        return score

    def _as_int(self, value: object) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _parse_json_response(self, content: str) -> dict[str, object]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
        if fenced_match:
            return json.loads(fenced_match.group(1))
        object_match = re.search(r"(\{.*\})", content, flags=re.DOTALL)
        if object_match:
            return json.loads(object_match.group(1))
        raise ValueError("AI response does not contain valid JSON")
