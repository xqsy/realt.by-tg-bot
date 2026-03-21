from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dataclasses import replace

from app.config import CITY_URLS, Settings
from app.models import Listing, SearchPageResult, SearchResult, UserPreferences

PRICE_RE = re.compile(r"(\d[\d \xa0]{0,15})\s*р\./мес\.", re.IGNORECASE)
USD_RE = re.compile(r"≈\s*(\d[\d \xa0]{0,10})\s*\$/мес\.", re.IGNORECASE)
PRICE_BLOCK_RE = re.compile(r"(\d[\d \xa0]{0,15})\s*р\./мес\.(?:\s*≈\s*(\d[\d \xa0]{0,10})\s*\$/мес\.)?", re.IGNORECASE)
ROOMS_RE = re.compile(r"(\d+)\s*комн", re.IGNORECASE)
AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*м²", re.IGNORECASE)
FLOOR_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*этаж", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{7,}\d")
DETAIL_PATH_RE = re.compile(r"^/(?:[a-z-]+/)?rent-flat-for-long/object/(\d+)/?$", re.IGNORECASE)
SECTION_HEADERS = ["Параметры объекта", "Удобства", "Примечание", "Арендодатель", "Местоположение"]
IGNORED_SECTION_LINES = {"Показать больше", "Скрыть", "Написать", "Показать контакты", "Контактное лицо"}
SECTION_STOP_LINES = {
    "Следить за ценой",
    "Номер договора",
    "Контактное лицо",
    "АН Гарант успеха",
}
AMENITY_BLACKLIST_PARTS = (
    "агентство недвижимости",
    "унп",
    "лицензия",
    "мю рб",
    "контактное лицо",
    "показать контакты",
    "написать",
    "следить за ценой",
    "договор",
)
PARAMETER_LABELS = [
    "Количество комнат",
    "Раздельных комнат",
    "Площадь общая",
    "Площадь жилая",
    "Площадь кухни",
    "Этаж / этажность",
    "Тип дома",
    "Ремонт",
    "Мебель",
    "Санузел",
    "Квартплата",
    "Срок аренды",
]
LOCATION_LABELS = [
    "Область",
    "Район",
    "Населенный пункт",
    "Улица",
    "Номер дома",
    "Район города",
    "Координаты",
]


class RealtParser:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, prefs: UserPreferences, limit: int | None = None) -> SearchResult:
        city_label, base_url = CITY_URLS[prefs.city_key]
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        page = 1
        while True:
            page_result = await self.search_page(prefs, page=page, seen_ids=seen_ids)
            listings.extend(page_result.items)
            if limit is not None and len(listings) >= limit:
                return SearchResult(items=listings[:limit], source_url=page_result.source_url)
            if not page_result.had_candidates:
                break
            page += 1
        return SearchResult(items=listings, source_url=base_url)

    async def search_page(self, prefs: UserPreferences, page: int, seen_ids: set[str] | None = None) -> SearchPageResult:
        city_label, base_url = CITY_URLS[prefs.city_key]
        page_url = base_url if page == 1 else f"{base_url}?page={page}"
        html = await self._fetch(page_url)
        page_listings = self._extract_listings_from_page(html, base_url, city_label)
        collected: list[Listing] = []
        seen = seen_ids if seen_ids is not None else set()
        for listing in page_listings:
            if listing.listing_id in seen:
                continue
            if not self._match_filters(listing, prefs):
                continue
            seen.add(listing.listing_id)
            detailed = await self._enrich_listing(listing)
            collected.append(detailed)
        return SearchPageResult(
            items=collected,
            page=page,
            source_url=page_url,
            had_candidates=bool(page_listings),
        )

    async def _fetch(self, url: str) -> str:
        response = await self._client.get(url)
        response.raise_for_status()
        return response.text

    def _extract_listings_from_page(self, html: str, base_url: str, city_label: str) -> list[Listing]:
        candidates: list[Listing] = []
        candidates.extend(self._extract_listings_from_json(html, city_label))
        if candidates:
            return self._deduplicate(candidates)
        candidates.extend(self._extract_listings_from_html(html, base_url, city_label))
        return self._deduplicate(candidates)

    def _extract_listings_from_json(self, html: str, city_label: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []
        for script in soup.find_all("script"):
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            script_id = script.get("id")
            script_type = script.get("type")
            json_payloads = self._extract_json_payloads(raw, script_id=script_id, script_type=script_type)
            for payload in json_payloads:
                for item in self._walk(payload):
                    if not isinstance(item, dict):
                        continue
                    normalized = self._normalize_listing_dict(item, city_label)
                    if normalized is not None:
                        listings.append(normalized)
        return listings

    def _extract_json_payloads(self, raw: str, script_id: str | None = None, script_type: str | None = None) -> list[object]:
        payloads: list[object] = []
        stripped = raw.strip()
        if script_id == "__NEXT_DATA__" or script_type == "application/ld+json" or stripped.startswith("{") or stripped.startswith("["):
            try:
                payloads.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return payloads

    def _walk(self, value: object) -> Iterable[object]:
        yield value
        if isinstance(value, dict):
            for nested in value.values():
                yield from self._walk(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from self._walk(nested)

    def _normalize_listing_dict(self, data: dict[str, object], city_label: str) -> Listing | None:
        url = self._extract_str(data, ["url", "href", "link", "fullUrl"])
        title = self._extract_str(data, ["title", "name", "header", "headline"])
        description = self._extract_str(data, ["description", "text", "body", "headline"])
        address = self._extract_address(data) or self._build_listing_address(data)
        price_byn = self._extract_int(data, ["price", "priceByn", "priceBYN", "priceValue"])
        price_usd = self._extract_int(data, ["priceUsd", "priceUSD"])
        rooms = self._extract_int(data, ["rooms", "roomCount"])
        area_m2 = self._extract_float(data, ["area", "areaTotal", "totalArea"])
        code_value = data.get("code")
        numeric_code: str | None = None
        if isinstance(code_value, int):
            numeric_code = str(code_value)
        elif isinstance(code_value, str) and code_value.strip().isdigit():
            numeric_code = code_value.strip()
        listing_id = numeric_code or self._extract_str(data, ["id", "objectId", "uuid", "code"])
        phone_numbers = self._extract_phone_list(data)
        photo_urls = self._extract_photo_urls(data)
        metro = self._extract_str(data, ["metro", "subway"])
        district = self._extract_str(data, ["district", "microdistrict", "stateDistrictName", "townDistrictName"])
        contact_name = self._extract_contact_name(data)
        published_at = self._extract_str(data, ["publishedAt", "createdAt", "created"])
        floor = self._extract_int(data, ["floor", "storey"])
        floors_total = self._extract_int(data, ["floors", "floorTotal", "floorsTotal", "storeys"])
        if not url and numeric_code:
            url = self._build_detail_url(data, numeric_code)
        if not (url or title or address):
            return None
        if url and not url.startswith("http"):
            url = urljoin("https://realt.by", url)
        if not self._looks_like_detail_url(url):
            return None
        if not title:
            title = self._build_title_from_parts(rooms, area_m2, address)
        if not listing_id:
            listing_id = self._make_listing_id(url=url, title=title, address=address)
        return Listing(
            listing_id=listing_id,
            url=url,
            title=title or "Объявление без названия",
            city_label=city_label,
            price_byn=price_byn,
            price_usd=price_usd,
            rooms=rooms,
            area_m2=area_m2,
            floor=floor,
            floors_total=floors_total,
            address=address,
            district=district,
            metro=metro,
            description=description,
            phone_numbers=phone_numbers,
            contact_name=contact_name,
            published_at=published_at,
            photo_urls=photo_urls,
            attributes=self._collect_attributes(data),
        )

    def _extract_listings_from_html(self, html: str, base_url: str, city_label: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            url = urljoin(base_url, href)
            if not self._looks_like_detail_url(url):
                continue
            text = " ".join(anchor.stripped_strings)
            if not text:
                continue
            listing_id = self._make_listing_id(url=url, title=text, address=None)
            listings.append(
                Listing(
                    listing_id=listing_id,
                    url=url,
                    title=text[:120],
                    city_label=city_label,
                )
            )
        return listings

    async def _enrich_listing(self, listing: Listing) -> Listing:
        try:
            html = await self._fetch(listing.url)
        except httpx.HTTPError:
            return listing
        detail_listing = self._extract_listing_from_detail_page(html, listing.url, listing.city_label)
        detail_candidates = self._extract_listings_from_json(html, listing.city_label)
        for candidate in detail_candidates:
            if candidate.url == listing.url or candidate.listing_id == listing.listing_id:
                merged = self._merge_listings(listing, candidate)
                return self._merge_listings(merged, detail_listing)
        merged = self._merge_with_detail_text(listing, html)
        return self._merge_listings(merged, detail_listing)

    def _merge_with_detail_text(self, listing: Listing, html: str) -> Listing:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        merged = replace(listing)
        if merged.price_byn is None:
            price_match = PRICE_RE.search(text)
            if price_match:
                merged.price_byn = self._clean_int(price_match.group(1))
        if merged.price_usd is None:
            usd_match = USD_RE.search(text)
            if usd_match:
                merged.price_usd = self._clean_int(usd_match.group(1))
        if merged.rooms is None:
            rooms_match = ROOMS_RE.search(text)
            if rooms_match:
                merged.rooms = int(rooms_match.group(1))
        if merged.area_m2 is None:
            area_match = AREA_RE.search(text)
            if area_match:
                merged.area_m2 = float(area_match.group(1).replace(",", "."))
        if merged.floor is None or merged.floors_total is None:
            floor_match = FLOOR_RE.search(text)
            if floor_match:
                merged.floor = int(floor_match.group(1))
                merged.floors_total = int(floor_match.group(2))
        if not merged.phone_numbers:
            merged.phone_numbers = sorted({self._normalize_phone(phone) for phone in PHONE_RE.findall(text) if self._normalize_phone(phone)})
        if not merged.description:
            merged.description = self._first_meaningful_paragraph(soup)
        if not merged.address:
            merged.address = self._extract_address_from_text(text)
        merged.attributes = merged.attributes or {}
        return merged

    def _merge_listings(self, base: Listing, detailed: Listing) -> Listing:
        return Listing(
            listing_id=base.listing_id or detailed.listing_id,
            url=base.url or detailed.url,
            title=detailed.title or base.title,
            city_label=base.city_label,
            price_byn=detailed.price_byn if detailed.price_byn is not None else base.price_byn,
            price_usd=detailed.price_usd if detailed.price_usd is not None else base.price_usd,
            rooms=detailed.rooms if detailed.rooms is not None else base.rooms,
            area_m2=detailed.area_m2 if detailed.area_m2 is not None else base.area_m2,
            floor=detailed.floor if detailed.floor is not None else base.floor,
            floors_total=detailed.floors_total if detailed.floors_total is not None else base.floors_total,
            address=detailed.address or base.address,
            district=detailed.district or base.district,
            metro=detailed.metro or base.metro,
            description=detailed.description or base.description,
            phone_numbers=detailed.phone_numbers or base.phone_numbers,
            contact_name=detailed.contact_name or base.contact_name,
            published_at=detailed.published_at or base.published_at,
            photo_urls=detailed.photo_urls or base.photo_urls,
            attributes={**base.attributes, **detailed.attributes},
        )

    def _match_filters(self, listing: Listing, prefs: UserPreferences) -> bool:
        if prefs.min_price is not None:
            if listing.price_byn is None or listing.price_byn < prefs.min_price:
                return False
        if prefs.max_price is not None:
            if listing.price_byn is None or listing.price_byn > prefs.max_price:
                return False
        if prefs.rooms is not None:
            if listing.rooms is None or listing.rooms != prefs.rooms:
                return False
        return True

    def _deduplicate(self, listings: list[Listing]) -> list[Listing]:
        unique: dict[str, Listing] = {}
        for listing in listings:
            unique[listing.listing_id] = listing
        return list(unique.values())

    def _looks_like_detail_url(self, url: str | None) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.netloc and "realt.by" not in parsed.netloc:
            return False
        path = parsed.path.rstrip("/")
        return DETAIL_PATH_RE.fullmatch(path) is not None

    def _extract_listing_from_detail_page(self, html: str, url: str, city_label: str) -> Listing:
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text("\n", strip=True)
        detail_object = self._extract_next_data_object(soup)
        price_byn, price_usd = self._extract_price_pair(page_text)
        title = self._extract_page_title(soup) or "Объявление без названия"
        parameter_lines = self._extract_section_lines(page_text, "Параметры объекта")
        location_lines = self._extract_section_lines(page_text, "Местоположение")
        landlord_lines = self._extract_section_lines(page_text, "Арендодатель")
        note_lines = self._extract_section_lines(page_text, "Примечание")
        parameters = self._parse_labeled_section(parameter_lines, PARAMETER_LABELS)
        amenities = self._extract_section_values(page_text, "Удобства")
        location = self._parse_labeled_section(location_lines, LOCATION_LABELS)
        address = self._build_address_from_location(location) or self._extract_address_from_title(title)
        district = self._first_non_empty(location.get("Район города"), location.get("Район"))
        rooms = self._clean_int(parameters.get("Количество комнат", "")) if parameters else None
        area_m2 = self._extract_float({"value": parameters.get("Площадь общая")} if parameters else {}, ["value"])
        floor, floors_total = self._parse_floor_pair(parameters.get("Этаж / этажность")) if parameters else (None, None)
        attributes: dict[str, object] = {}
        if parameters:
            attributes["Параметры объекта"] = parameters
        if amenities:
            attributes["Удобства"] = amenities
        if location:
            attributes["Местоположение"] = location
        return Listing(
            listing_id=self._make_listing_id(url=url, title=title, address=address),
            url=url,
            title=title,
            city_label=city_label,
            price_byn=price_byn,
            price_usd=price_usd,
            rooms=rooms,
            area_m2=area_m2,
            floor=floor,
            floors_total=floors_total,
            address=address,
            district=district,
            description=self._join_section_lines(note_lines),
            phone_numbers=self._extract_detail_phone_numbers(detail_object) or self._extract_phone_numbers_from_lines(landlord_lines),
            contact_name=self._extract_contact_name_from_lines(landlord_lines),
            attributes=attributes,
        )

    def _extract_page_title(self, soup: BeautifulSoup) -> str | None:
        heading = soup.find(["h1", "title"])
        if heading is None:
            return None
        text = " ".join(heading.stripped_strings)
        if not text:
            return None
        text = text.split(" | ", 1)[0].strip()
        text = re.sub(r"\s+id\d+\s*$", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _extract_section_lines(self, text: str, section_header: str) -> list[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        section_lines: list[str] = []
        in_section = False
        for line in lines:
            if line == section_header:
                in_section = True
                continue
            if in_section and (line in SECTION_HEADERS or line in SECTION_STOP_LINES):
                break
            if in_section:
                section_lines.append(line)
        return section_lines

    def _extract_section_values(self, text: str, section_header: str) -> list[str]:
        values: list[str] = []
        for line in self._extract_section_lines(text, section_header):
            cleaned = line.lstrip("- ").strip()
            if not cleaned or cleaned in IGNORED_SECTION_LINES or self._should_skip_amenity(cleaned):
                continue
            if cleaned not in values:
                values.append(cleaned)
        return values

    def _parse_labeled_section(self, lines: list[str], labels: list[str]) -> dict[str, str]:
        block = "\n".join(lines)
        result: dict[str, str] = {}
        if not block:
            return result
        escaped_labels = "|".join(re.escape(label) for label in labels)
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*[:\-]?\s*(.+?)(?=\n(?:{escaped_labels})\s*[:\-]?|\Z)", block, re.S)
            if not match:
                continue
            value = self._clean_section_value(match.group(1))
            if value:
                result[label] = value
        return result

    def _clean_section_value(self, value: str) -> str | None:
        lines = [line.strip(" -") for line in value.splitlines() if line.strip()]
        if not lines:
            return None
        return lines[0]

    def _should_skip_amenity(self, value: str) -> bool:
        lowered = value.lower()
        if lowered in {"-", ",", "."}:
            return True
        if any(part in lowered for part in AMENITY_BLACKLIST_PARTS):
            return True
        digits_only = re.sub(r"\D", "", value)
        if digits_only and re.fullmatch(r"\d{8,}", digits_only):
            return True
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
            return True
        return False

    def _build_address_from_location(self, location: dict[str, str]) -> str | None:
        parts = [location.get("Населенный пункт"), location.get("Улица"), location.get("Номер дома")]
        normalized = [part for part in parts if part]
        return ", ".join(normalized) if normalized else None

    def _extract_address_from_title(self, title: str) -> str | None:
        parts = [part.strip() for part in title.split(",")]
        if len(parts) < 3:
            return None
        return ", ".join(parts[1:]).strip()

    def _extract_contact_name_from_lines(self, lines: list[str]) -> str | None:
        for line in lines:
            if line in {"Контактное лицо", "Арендодатель"} or line in IGNORED_SECTION_LINES:
                continue
            if PHONE_RE.search(line):
                continue
            if len(line.split()) <= 5:
                return line
        return None

    def _extract_phone_numbers_from_lines(self, lines: list[str]) -> list[str]:
        numbers: list[str] = []
        for line in lines:
            if line in IGNORED_SECTION_LINES:
                continue
            for phone in PHONE_RE.findall(line):
                normalized = self._normalize_phone(phone)
                if normalized and normalized not in numbers:
                    numbers.append(normalized)
        return numbers

    def _extract_next_data_object(self, soup: BeautifulSoup) -> dict[str, object] | None:
        script = soup.find("script", id="__NEXT_DATA__")
        if script is None:
            return None
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        props = payload.get("props")
        if not isinstance(props, dict):
            return None
        page_props = props.get("pageProps")
        if not isinstance(page_props, dict):
            return None
        detail_object = page_props.get("object")
        return detail_object if isinstance(detail_object, dict) else None

    def _extract_detail_phone_numbers(self, detail_object: dict[str, object] | None) -> list[str]:
        if not isinstance(detail_object, dict):
            return []
        value = detail_object.get("contactPhones")
        numbers: list[str] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    normalized = self._normalize_phone(item)
                    if normalized and normalized not in numbers:
                        numbers.append(normalized)
        elif isinstance(value, str):
            normalized = self._normalize_phone(value)
            if normalized:
                numbers.append(normalized)
        return numbers

    def _extract_price_pair(self, text: str) -> tuple[int | None, int | None]:
        match = PRICE_BLOCK_RE.search(text)
        if not match:
            return self._extract_price_byn(text), self._extract_price_usd(text)
        price_byn = self._clean_int(match.group(1))
        price_usd = self._clean_int(match.group(2)) if match.group(2) else None
        return price_byn, price_usd

    def _extract_price_byn(self, text: str) -> int | None:
        match = PRICE_BLOCK_RE.search(text)
        if match:
            return self._clean_int(match.group(1))
        match = PRICE_RE.search(text)
        return self._clean_int(match.group(1)) if match else None

    def _extract_price_usd(self, text: str) -> int | None:
        match = USD_RE.search(text)
        return self._clean_int(match.group(1)) if match else None

    def _parse_floor_pair(self, value: str | None) -> tuple[int | None, int | None]:
        if not value:
            return None, None
        match = re.search(r"(\d+)\s*/\s*(\d+)", value)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _join_section_lines(self, lines: list[str]) -> str | None:
        cleaned = [line for line in lines if line not in SECTION_HEADERS and line not in IGNORED_SECTION_LINES]
        if not cleaned:
            return None
        return " ".join(cleaned)[:1800]

    def _first_non_empty(self, *values: str | None) -> str | None:
        for value in values:
            if value:
                return value
        return None

    def _extract_str(self, data: dict[str, object], keys: list[str]) -> str | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_int(self, data: dict[str, object], keys: list[str]) -> int | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                cleaned = self._clean_int(value)
                if cleaned is not None:
                    return cleaned
        return None

    def _extract_float(self, data: dict[str, object], keys: list[str]) -> float | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                cleaned = re.sub(r"[^\d,\.]", "", value)
                if cleaned:
                    try:
                        return float(cleaned.replace(",", "."))
                    except ValueError:
                        continue
        return None

    def _extract_address(self, data: dict[str, object]) -> str | None:
        value = data.get("address")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            pieces: list[str] = []
            for key in ("city", "district", "street", "house", "label", "full"):
                part = value.get(key)
                if isinstance(part, str) and part.strip():
                    pieces.append(part.strip())
            if pieces:
                return ", ".join(pieces)
        return None

    def _build_listing_address(self, data: dict[str, object]) -> str | None:
        parts: list[str] = []
        town_name = data.get("townName")
        if isinstance(town_name, str) and town_name.strip():
            parts.append(f"г. {town_name.strip()}")
        street_name = data.get("streetName")
        if isinstance(street_name, str) and street_name.strip():
            street = street_name.strip()
            house_number = data.get("houseNumber")
            if house_number is not None and str(house_number).strip():
                street = f"{street}, {str(house_number).strip()}"
            parts.append(street)
        if not parts:
            return None
        return ", ".join(parts)

    def _build_detail_url(self, data: dict[str, object], listing_id: str) -> str:
        region_slug = self._extract_str(data, ["regionSlug"])
        if not region_slug:
            region_url = self._extract_str(data, ["stateRegionUrl"])
            if region_url:
                match = re.match(r"/([a-z-]+-region)/", region_url)
                if match:
                    region_slug = match.group(1)
        if not region_slug:
            region_name = self._extract_str(data, ["stateRegionName"])
            region_map = {
                "Минск": "",
                "Брестская область": "brest-region",
                "Могилевская область": "mogilev-region",
                "Гомельская область": "gomel-region",
                "Гродненская область": "grodno-region",
                "Витебская область": "vitebsk-region",
            }
            region_slug = region_map.get(region_name or "", "")
        if region_slug:
            return f"https://realt.by/{region_slug}/rent-flat-for-long/object/{listing_id}/"
        return f"https://realt.by/rent-flat-for-long/object/{listing_id}/"

    def _extract_phone_list(self, data: dict[str, object]) -> list[str]:
        results: set[str] = set()
        for key, value in data.items():
            key_lower = key.lower()
            if "phone" in key_lower:
                if isinstance(value, str):
                    normalized = self._normalize_phone(value)
                    if normalized:
                        results.add(normalized)
                    for phone in PHONE_RE.findall(value):
                        normalized = self._normalize_phone(phone)
                        if normalized:
                            results.add(normalized)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            normalized = self._normalize_phone(item)
                            if normalized:
                                results.add(normalized)
                            for phone in PHONE_RE.findall(item):
                                normalized = self._normalize_phone(phone)
                                if normalized:
                                    results.add(normalized)
                        elif isinstance(item, dict):
                            results.update(self._extract_phone_list(item))
                elif isinstance(value, dict):
                    results.update(self._extract_phone_list(value))
                continue
            if key_lower in {"contact", "agent", "owner", "seller"} and isinstance(value, dict):
                results.update(self._extract_phone_list(value))
        return sorted(results)

    def _extract_photo_urls(self, data: dict[str, object]) -> list[str]:
        urls: set[str] = set()
        for key, value in data.items():
            key_lower = key.lower()
            if isinstance(value, str) and value.startswith("http") and any(token in key_lower for token in ("photo", "image", "img", "slide")):
                urls.add(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith("http"):
                        urls.add(item)
                    elif isinstance(item, dict):
                        urls.update(self._extract_photo_urls(item))
            elif isinstance(value, dict):
                urls.update(self._extract_photo_urls(value))
        return sorted(urls)

    def _extract_contact_name(self, data: dict[str, object]) -> str | None:
        for key in ("contactName", "ownerName", "authorName", "name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip() and len(value.strip().split()) <= 5:
                return value.strip()
        contact = data.get("contact")
        if isinstance(contact, dict):
            return self._extract_contact_name(contact)
        return None

    def _collect_attributes(self, data: dict[str, object]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key in ("sellerType", "repair", "furniture", "balcony", "pets", "children", "bathroom", "heating", "internet"):
            value = data.get(key)
            if value is not None:
                selected[key] = value
        return selected

    def _build_title_from_parts(self, rooms: int | None, area_m2: float | None, address: str | None) -> str | None:
        parts: list[str] = []
        if rooms is not None:
            parts.append(f"{rooms}-комн. квартира")
        if area_m2 is not None:
            parts.append(f"{area_m2:g} м²")
        if address:
            parts.append(address)
        return ", ".join(parts) if parts else None

    def _make_listing_id(self, url: str | None, title: str | None, address: str | None) -> str:
        if url:
            parsed = urlparse(url)
            match = DETAIL_PATH_RE.fullmatch(parsed.path.rstrip("/"))
            if match:
                return match.group(1)
        base = url or title or address or "listing"
        return re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()[:80] or "listing"

    def _clean_int(self, value: str) -> int | None:
        digits = re.sub(r"\D", "", value)
        return int(digits) if digits else None

    def _normalize_phone(self, value: str) -> str | None:
        cleaned = re.sub(r"[^\d+]", "", value)
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) < 9:
            return None
        return f"+{digits}" if not cleaned.startswith("+") else f"+{digits}"

    def _extract_address_from_text(self, text: str) -> str | None:
        match = re.search(r"г\.\s*[А-Яа-яЁё\-\s]+,\s*[^\n]+", text)
        if match:
            return match.group(0).strip()
        return None

    def _first_meaningful_paragraph(self, soup: BeautifulSoup) -> str | None:
        for tag in soup.find_all(["p", "div", "span"]):
            text = " ".join(tag.stripped_strings)
            if len(text) >= 80:
                return text[:1500]
        return None
