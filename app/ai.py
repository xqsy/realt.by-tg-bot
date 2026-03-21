from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.config import CITY_URLS, Settings
from app.models import Listing, UserPreferences


@dataclass(slots=True)
class QueryAnalysis:
    original_query: str
    intent: str = "replace"
    city_key: str | None = None
    min_price: int | None = None
    max_price: int | None = None
    rooms: int | None = None
    features: list[str] | None = None
    summary: str = ""
    ai_available: bool = True

    def has_updates(self) -> bool:
        return any(value is not None for value in (self.city_key, self.min_price, self.max_price, self.rooms)) or bool(self.features)


class HousingQueryAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze(self, query: str, current_prefs: UserPreferences) -> QueryAnalysis:
        if not self._settings.ai_api_key or not self._settings.ai_model:
            return QueryAnalysis(original_query=query, ai_available=False)
        try:
            return await self._remote_parse(query, current_prefs)
        except Exception as exc:
            logging.warning("AI query analysis fallback triggered: %s", exc)
            return QueryAnalysis(original_query=query, ai_available=False)

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
            "Верни только JSON с полями intent, city_key, min_price, max_price, rooms, features, summary. "
            "intent должен быть replace, если пользователь начинает новый поиск с новыми критериями, "
            "или refine, если он дополняет или уточняет предыдущий запрос. "
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
        intent = str(parsed.get("intent") or "replace").strip().lower()
        if intent not in {"replace", "refine"}:
            intent = "replace"
        features = parsed.get("features")
        if not isinstance(features, list):
            features = []
        return QueryAnalysis(
            original_query=query,
            intent=intent,
            city_key=city_key,
            min_price=self._as_int(parsed.get("min_price")),
            max_price=self._as_int(parsed.get("max_price")),
            rooms=self._as_int(parsed.get("rooms")),
            features=[str(item).strip().lower() for item in features if str(item).strip()],
            summary=str(parsed.get("summary") or "").strip(),
            ai_available=True,
        )

    def _score_listing(self, listing: Listing, analysis: QueryAnalysis, prefs: UserPreferences) -> float:
        score = 0.0
        features = analysis.features or []
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
        for feature in features:
            if feature and feature in haystack:
                score += 2.0
        if listing.metro and any("метро" in feature for feature in features):
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
