from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from core.config import CITY_URLS, Settings
from core.models import Listing, UserPreferences


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
            "intent должен быть replace, если пользователь начинает новый поиск с новыми критериями; "
            "refine, если он дополняет или уточняет предыдущий запрос; "
            "off_topic, если запрос вообще не связан с поиском квартиры в аренду "
            "(например, вопросы о погоде, новостях, помощь с кодом и т.д.). "
            "При off_topic в summary напиши дружелюбный ответ, что ты помогаешь только с поиском квартир, "
            "и предложи написать запрос типа \"двушка в Минске до 1200\". "
            "city_key может быть только одним из: "
            f"{city_options}. "
            "Распознавай город в любой грамматической форме: "
            "минск/минске/минска/в минск → minsk, "
            "брест/бресте/в брест → brest, "
            "могилев/могилеве/в могилев → mogilev, "
            "гомель/гомеле/в гомель → gomel, "
            "гродно/в гродно → grodno, "
            "витебск/витебске/в витебск → vitebsk. "
            "Если город упомянут, обязательно верни city_key — не оставляй null. "
            "Цены всегда возвращай в белорусских рублях (BYN), не в долларах. "
            "Слово 'тысяча', 'тысячу', 'тыща', 'тыщу', 'тыс', 'к' после числа означает ×1000: "
            "'тыщу рублей' = 1000, '1.5 тыщи' = 1500, '2к' = 2000. "
            "Если пользователь пишет 'до 1200', 'за 1200', 'не дороже 1200' или просто указывает бюджет без нижней границы, "
            "то заполняй только max_price=1200, а min_price оставляй null. "
            "min_price заполняй только если пользователь явно задал нижнюю границу, например 'от 800'. "
            "rooms — только целое число комнат (1, 2, 3...), либо null. "
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
        if city_key is None:
            city_key = self._detect_city_from_query(query)
        intent = str(parsed.get("intent") or "replace").strip().lower()
        if intent not in {"replace", "refine", "off_topic"}:
            intent = "replace"
        features = parsed.get("features")
        if not isinstance(features, list):
            features = []
        min_price = self._as_int(parsed.get("min_price"))
        max_price = self._as_int(parsed.get("max_price"))
        min_price = self._fix_colloquial_price(query, min_price)
        max_price = self._fix_colloquial_price(query, max_price)
        min_price, max_price = self._normalize_prices(query, min_price, max_price)
        return QueryAnalysis(
            original_query=query,
            intent=intent,
            city_key=city_key,
            min_price=min_price,
            max_price=max_price,
            rooms=self._as_int(parsed.get("rooms")),
            features=[str(item).strip().lower() for item in features if str(item).strip()],
            summary=str(parsed.get("summary") or "").strip(),
            ai_available=True,
        )

    def _score_listing(self, listing: Listing, analysis: QueryAnalysis, prefs: UserPreferences) -> float:
        score = 0.0
        features = analysis.features or []
        if analysis.rooms is not None:
            if listing.rooms == analysis.rooms:
                score += 3.0
            elif listing.rooms is not None:
                score -= abs(listing.rooms - analysis.rooms) * 1.5
        elif prefs.rooms is not None:
            if listing.rooms in prefs.rooms:
                score += 3.0
            elif listing.rooms is not None:
                min_diff = min(abs(listing.rooms - r) for r in prefs.rooms)
                score -= min_diff * 1.5
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

    _CITY_PATTERNS: dict[str, re.Pattern[str]] = {
        "minsk":   re.compile(r"\bминск[аеу]?\b", re.IGNORECASE),
        "brest":   re.compile(r"\bбрест[еу]?\b", re.IGNORECASE),
        "mogilev": re.compile(r"\bмогилев[еу]?\b", re.IGNORECASE),
        "gomel":   re.compile(r"\bгомел[ьея]\b", re.IGNORECASE),
        "grodno":  re.compile(r"\bгродн[оа]\b", re.IGNORECASE),
        "vitebsk": re.compile(r"\bвитебск[еу]?\b", re.IGNORECASE),
    }
    _KILO_PRICE_RE = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*(?:тысяч[аую]?|тыщ[аую]?|тыс\.?|к(?=\s|$))",
        re.IGNORECASE,
    )

    def _detect_city_from_query(self, query: str) -> str | None:
        for city_key, pattern in self._CITY_PATTERNS.items():
            if pattern.search(query):
                return city_key
        return None

    def _fix_colloquial_price(self, query: str, price: int | None) -> int | None:
        if price is None:
            return None
        q = query.lower()
        for match in self._KILO_PRICE_RE.finditer(q):
            raw = match.group(1).replace(",", ".")
            try:
                candidate = round(float(raw) * 1000)
                unscaled = round(float(raw))
            except ValueError:
                continue
            if abs(price - unscaled) <= max(1, unscaled * 0.05):
                return candidate
        if price >= 10000 and re.search(
            r"(?<![0-9.,])\s*(?:тысяч[аую]|тысяч[еи]|тыщ[аую])\b", q
        ):
            return 1000
        return price

    def _as_int(self, value: object) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _normalize_prices(self, query: str, min_price: int | None, max_price: int | None) -> tuple[int | None, int | None]:
        normalized_query = query.lower()
        budget_without_lower_bound = re.search(
            r"(?:\bдо\s+\d|\bза\s+\d|\bне\s+дороже\s+\d|\bне\s+больше\s+\d|\bбюджет\s+\d)",
            normalized_query,
        )
        explicit_lower_bound = re.search(r"\bот\s+\d", normalized_query)
        if min_price is not None and max_price is not None and min_price > max_price:
            min_price, max_price = max_price, min_price
        if budget_without_lower_bound and not explicit_lower_bound:
            if max_price is None and min_price is not None:
                max_price = min_price
                min_price = None
            elif min_price is not None and max_price is not None and min_price == max_price:
                min_price = None
        return min_price, max_price

    async def generate_listing_intro(
        self,
        listing: Listing,
        user_query: str,
        result_count: int,
        analysis_summary: str = "",
    ) -> str | None:
        if not self._settings.ai_api_key or not self._settings.ai_model:
            return None
        try:
            return await self._call_listing_intro(listing, user_query, result_count, analysis_summary)
        except Exception as exc:
            logging.warning("AI listing intro failed: %s", exc)
            return None

    async def _call_listing_intro(
        self,
        listing: Listing,
        user_query: str,
        result_count: int,
        analysis_summary: str,
    ) -> str:
        listing_info = self._listing_brief_text(listing)
        context = (
            f"Запрос пользователя: «{user_query}»\n"
            + (f"Распознано: {analysis_summary}\n" if analysis_summary else "")
            + f"Результатов найдено: {result_count}\n"
            f"Первое объявление:\n{listing_info}\n\n"
            "Напиши краткое (1-2 предложения) дружелюбное сообщение, представляющее это объявление. "
            "Без Markdown, без эмодзи."
        )
        payload = {
            "model": self._settings.ai_model,
            "temperature": 0.6,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты дружелюбный ассистент по аренде квартир в Беларуси. "
                        "Отвечай по-русски, кратко, без форматирования."
                    ),
                },
                {"role": "user", "content": context},
            ],
        }
        async with httpx.AsyncClient(timeout=self._settings.request_timeout) as client:
            response = await client.post(
                f"{self._settings.ai_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._settings.ai_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com",
                    "X-Title": "realt-bot",
                },
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        content = str(data["choices"][0]["message"].get("content") or "").strip()
        return content if content else None

    def _listing_brief_text(self, listing: Listing) -> str:
        parts = [f"Название: {listing.title}", f"Цена: {listing.price_label}"]
        if listing.address:
            parts.append(f"Адрес: {listing.address}")
        if listing.rooms is not None:
            parts.append(f"Комнат: {listing.rooms}")
        if listing.area_m2 is not None:
            parts.append(f"Площадь: {listing.area_m2} м²")
        if listing.floor is not None and listing.floors_total is not None:
            parts.append(f"Этаж: {listing.floor} из {listing.floors_total}")
        if listing.district:
            parts.append(f"Район: {listing.district}")
        if listing.metro:
            parts.append(f"Метро: {listing.metro}")
        return "\n".join(parts)

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
