from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, replace
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel

from core.ai import HousingQueryAnalyzer, QueryAnalysis
from core.config import CITY_URLS, load_settings
from core.formatters import format_preferences
from core.models import Listing, UserPreferences
from core.parser import RealtParser

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
settings = load_settings()
SESSION_COOKIE_NAME = "realt_web_session"
SEARCH_STATE_KEY = "search_state"


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.parser = RealtParser(settings)
    application.state.query_analyzer = HousingQueryAnalyzer(settings)
    application.state.web_sessions = {}
    try:
        yield
    finally:
        await application.state.parser.close()


def create_app() -> FastAPI:
    application = FastAPI(title="Realt Search", lifespan=lifespan)

    @application.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/chat", status_code=301)

    @application.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        session = _get_session(request)
        logger.info("[chat] GET /chat session=%s", session_id[:8])
        response = templates.TemplateResponse("chat.html", {"request": request})
        _set_session_cookie(response, session_id)
        return response

    @application.post("/chat/message")
    async def chat_message(request: Request, body: ChatRequest) -> JSONResponse:
        session_id = _ensure_session_id(request)
        session = _get_session(request)
        text = body.text.strip()
        action = body.action.strip()
        logger.info(
            "[chat] POST /chat/message session=%s action=%r text=%r",
            session_id[:8],
            action or None,
            text[:80] if text else None,
        )
        messages = await _handle_chat_message(request, session, text or None, action or None)
        logger.info("[chat] response has %d message(s)", len(messages))
        response = JSONResponse({"messages": messages})
        _set_session_cookie(response, session_id)
        return response

    return application


class ChatRequest(BaseModel):
    text: str = ""
    action: str = ""


app = create_app()



def _parse_int(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def _parse_rooms(raw: str) -> int | None:
    value = raw.strip()
    return int(value) if value.isdigit() else None


def _deserialize_rooms_pref(value: object) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        items = [int(x) for x in value if isinstance(x, int) or (isinstance(x, str) and x.strip().isdigit())]
        return items if items else None
    return None


def _apply_query_analysis(prefs: UserPreferences, analysis: QueryAnalysis) -> UserPreferences:
    if analysis.intent == "replace":
        updated = UserPreferences(user_id=prefs.user_id, city_key=prefs.city_key)
    else:
        updated = replace(prefs)
    if analysis.city_key is not None:
        updated.city_key = analysis.city_key
    if analysis.min_price is not None:
        updated.min_price = analysis.min_price
    if analysis.max_price is not None:
        updated.max_price = analysis.max_price
    if analysis.rooms is not None:
        updated.rooms = [analysis.rooms]
    return updated



def _ensure_session_id(request: Request) -> str:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    sessions = request.app.state.web_sessions
    if session_id and session_id in sessions:
        return session_id
    session_id = uuid.uuid4().hex
    sessions[session_id] = {}
    return session_id


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(SESSION_COOKIE_NAME, session_id, httponly=True, samesite="lax")


def _get_session(request: Request) -> dict[str, object]:
    session_id = _ensure_session_id(request)
    sessions = request.app.state.web_sessions
    session = sessions.get(session_id)
    if isinstance(session, dict):
        return session
    sessions[session_id] = {}
    return sessions[session_id]


def _serialize_preferences(prefs: UserPreferences) -> dict[str, object]:
    return {
        "user_id": prefs.user_id,
        "city_key": prefs.city_key,
        "min_price": prefs.min_price,
        "max_price": prefs.max_price,
        "rooms": prefs.rooms,
    }


def _get_session_preferences(session: dict[str, object]) -> UserPreferences:
    raw = session.get("prefs")
    if isinstance(raw, dict):
        city_key = raw.get("city_key")
        normalized_city_key = city_key if isinstance(city_key, str) and city_key in CITY_URLS else "minsk"
        return UserPreferences(
            user_id=int(raw.get("user_id", 0) or 0),
            city_key=normalized_city_key,
            min_price=raw.get("min_price") if isinstance(raw.get("min_price"), int) else None,
            max_price=raw.get("max_price") if isinstance(raw.get("max_price"), int) else None,
            rooms=_deserialize_rooms_pref(raw.get("rooms")),
        )
    prefs = UserPreferences(user_id=0)
    session["prefs"] = _serialize_preferences(prefs)
    return prefs


def _serialize_query_analysis(analysis: QueryAnalysis) -> dict[str, object]:
    return {
        "original_query": analysis.original_query,
        "intent": analysis.intent,
        "city_key": analysis.city_key,
        "min_price": analysis.min_price,
        "max_price": analysis.max_price,
        "rooms": analysis.rooms,
        "features": analysis.features or [],
        "summary": analysis.summary,
        "ai_available": analysis.ai_available,
    }


def _get_query_analysis(session: dict[str, object]) -> QueryAnalysis | None:
    raw = session.get("query_analysis")
    if not isinstance(raw, dict):
        return None
    return QueryAnalysis(
        original_query=str(raw.get("original_query", "")),
        intent=str(raw.get("intent", "replace")),
        city_key=raw.get("city_key") if isinstance(raw.get("city_key"), str) else None,
        min_price=raw.get("min_price") if isinstance(raw.get("min_price"), int) else None,
        max_price=raw.get("max_price") if isinstance(raw.get("max_price"), int) else None,
        rooms=raw.get("rooms") if isinstance(raw.get("rooms"), int) else None,
        features=[str(item) for item in raw.get("features", [])] if isinstance(raw.get("features"), list) else [],
        summary=str(raw.get("summary", "")),
        ai_available=bool(raw.get("ai_available", False)),
    )


def _create_search_state(city_label: str, city_key: str) -> dict[str, object]:
    return {
        "results": [],
        "city_label": city_label,
        "city_key": city_key,
        "index": 0,
        "next_page": 1,
        "seen_ids": [],
        "exhausted": False,
        "max_page": None,
        "prefetch_task": None,
        "prefetch_in_progress": False,
    }


def _get_search_state(session: dict[str, object]) -> dict[str, object] | None:
    state = session.get(SEARCH_STATE_KEY)
    return state if isinstance(state, dict) else None


def _clear_search_state(session: dict[str, object]) -> None:
    state = _get_search_state(session)
    if state is not None:
        prefetch_task = state.get("prefetch_task")
        if isinstance(prefetch_task, asyncio.Task) and not prefetch_task.done():
            prefetch_task.cancel()
    session.pop(SEARCH_STATE_KEY, None)


def _deserialize_listing(data: object) -> Listing | None:
    if not isinstance(data, dict):
        return None
    try:
        return Listing(**data)
    except TypeError:
        return None


def _get_current_listing(session: dict[str, object]) -> Listing | None:
    state = _get_search_state(session)
    if state is None:
        return None
    results = state.get("results")
    index = state.get("index")
    if not isinstance(results, list) or not isinstance(index, int) or index < 0 or index >= len(results):
        return None
    return _deserialize_listing(results[index])


async def _load_next_search_page(request: Request, session: dict[str, object], prefs: UserPreferences) -> bool:
    state = _get_search_state(session)
    if state is None:
        return False
    results = state.get("results")
    next_page = state.get("next_page")
    exhausted = state.get("exhausted")
    max_page = state.get("max_page")
    raw_seen_ids = state.get("seen_ids")
    if not isinstance(results, list) or not isinstance(next_page, int) or not isinstance(exhausted, bool):
        return False
    if exhausted:
        return False
    if isinstance(max_page, int) and next_page > max_page:
        state["exhausted"] = True
        return False
    seen_ids = {str(item) for item in raw_seen_ids} if isinstance(raw_seen_ids, list) else set()
    while True:
        page_result = await request.app.state.parser.search_page(prefs, page=next_page, seen_ids=seen_ids)
        if isinstance(page_result.max_page, int):
            state["max_page"] = page_result.max_page
        state["next_page"] = next_page + 1
        state["seen_ids"] = sorted(seen_ids)
        if page_result.items:
            results.extend(asdict(item) for item in page_result.items)
            return True
        if not page_result.had_candidates or not page_result.had_unseen_candidates:
            state["exhausted"] = True
            return False
        if isinstance(page_result.max_page, int) and next_page >= page_result.max_page:
            state["exhausted"] = True
            return False
        next_page += 1


async def _perform_search(request: Request, session: dict[str, object], prefs: UserPreferences) -> None:
    city_label = CITY_URLS[prefs.city_key][0]
    session["search_error"] = ""
    session[SEARCH_STATE_KEY] = _create_search_state(city_label, prefs.city_key)
    try:
        loaded = await _load_next_search_page(request, session, prefs)
    except Exception as exc:
        logging.exception("Search failed", exc_info=exc)
        session["search_error"] = "Не удалось получить объявления с realt.by. Попробуйте позже."
        return
    state = _get_search_state(session)
    results = state.get("results") if state is not None else None
    if not loaded or not isinstance(results, list) or not results:
        session["search_error"] = "По вашим параметрам объявления не найдены. Попробуйте сменить город или ослабить фильтры."
        _clear_search_state(session)
        return
    analysis = _get_query_analysis(session)
    if analysis is not None:
        ranked_results = request.app.state.query_analyzer.rank_listings(
            [_deserialize_listing(item) for item in results if _deserialize_listing(item) is not None],
            analysis,
            prefs,
        )
        state["results"] = [asdict(item) for item in ranked_results]
    state["index"] = 0


async def _move_search_index(request: Request, session: dict[str, object], prefs: UserPreferences, step: int) -> None:
    state = _get_search_state(session)
    if state is None:
        session["search_error"] = "Сначала выполните поиск объявлений."
        return
    results = state.get("results")
    current_index = state.get("index", 0)
    exhausted = state.get("exhausted", False)
    if not isinstance(results, list) or not isinstance(current_index, int) or not isinstance(exhausted, bool):
        session["search_error"] = "Сначала выполните поиск объявлений."
        return
    new_index = current_index + step
    if new_index < 0:
        session["search_error"] = "Это первое объявление."
        return
    if new_index >= len(results):
        if exhausted:
            session["search_error"] = "Больше квартир по текущему запросу не найдено."
            return
        prefetch_task = state.get("prefetch_task")
        if isinstance(prefetch_task, asyncio.Task) and not prefetch_task.done():
            logger.info("[search] waiting for background prefetch task")
            try:
                await prefetch_task
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("[search] prefetch task finished with: %s", exc)
            state = _get_search_state(session)
            results = state.get("results") if state is not None else None
            if isinstance(results, list) and new_index < len(results):
                state["index"] = new_index
                session["search_error"] = ""
                return
        try:
            loaded = await _load_next_search_page(request, session, prefs)
        except Exception as exc:
            logging.exception("Failed to load next search page", exc_info=exc)
            session["search_error"] = "Не удалось загрузить следующую страницу объявлений."
            return
        state = _get_search_state(session)
        results = state.get("results") if state is not None else None
        analysis = _get_query_analysis(session)
        if analysis is not None and state is not None and isinstance(results, list):
            deserialized_results = [
                _deserialize_listing(item) for item in results if _deserialize_listing(item) is not None
            ]
            ranked_results = request.app.state.query_analyzer.rank_listings(deserialized_results, analysis, prefs)
            state["results"] = [asdict(item) for item in ranked_results]
            results = state["results"]
        if not loaded or not isinstance(results, list) or new_index >= len(results):
            session["search_error"] = "Больше квартир по текущему запросу не найдено."
            return
    state["index"] = new_index
    session["search_error"] = ""


async def _handle_chat_message(
    request: Request,
    session: dict[str, object],
    text: str | None,
    action: str | None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    prefs = _get_session_preferences(session)

    if action == "start":
        session["chat_pending_filter"] = None
        _clear_search_state(session)
        logger.info("[chat] start action triggered")
        messages.append({
            "type": "text",
            "content": (
                "Привет! Я помогу подобрать квартиру в долгосрочную аренду.\n\n"
                "Можно написать запрос в свободной форме, например: «двушка в Минске до 1200 рядом с метро», "
                "или выбрать город и настроить фильтры через кнопки."
            ),
        })
        messages.append({"type": "text", "content": "Выберите город для начала:"})
        messages.append({"type": "buttons", "buttons": _city_button_list()})
        return messages

    if action and action.startswith("city:"):
        city_key = action.split(":", 1)[1]
        if city_key in CITY_URLS:
            prefs.city_key = city_key
            session["prefs"] = _serialize_preferences(prefs)
            _clear_search_state(session)
            city_label = CITY_URLS[city_key][0]
            logger.info("[chat] city changed to %s", city_key)
            messages.append({"type": "text", "content": f"Переключил поиск на {city_label}."})
            messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    if action == "menu:city":
        messages.append({"type": "text", "content": "Выберите город:"})
        messages.append({"type": "buttons", "buttons": _city_button_list()})
        return messages

    if action == "menu:filters":
        city_label = CITY_URLS[prefs.city_key][0]
        messages.append({"type": "text", "content": format_preferences(prefs, city_label)})
        messages.append({"type": "buttons", "buttons": _filter_buttons(prefs)})
        return messages

    if action == "menu:rooms":
        messages.append({"type": "text", "content": "Выберите количество комнат:"})
        messages.append({"type": "buttons", "buttons": _rooms_buttons()})
        return messages

    if action == "search":
        logger.info("[chat] manual search triggered")
        return await _do_chat_search(request, session, prefs, None, None, messages)

    if action == "reset":
        prefs.min_price = None
        prefs.max_price = None
        prefs.rooms = None
        session["prefs"] = _serialize_preferences(prefs)
        session.pop("query_analysis", None)
        _clear_search_state(session)
        city_label = CITY_URLS[prefs.city_key][0]
        logger.info("[chat] filters reset")
        messages.append({"type": "text", "content": f"Фильтры сброшены.\nГород: {city_label}."})
        messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    if action == "filter:min_price":
        session["chat_pending_filter"] = "min_price"
        messages.append({"type": "text", "content": "Введите минимальную цену в BYN, например: 500"})
        return messages

    if action == "filter:max_price":
        session["chat_pending_filter"] = "max_price"
        messages.append({"type": "text", "content": "Введите максимальную цену в BYN, например: 1200"})
        return messages

    if action == "filter:clear_price":
        prefs.min_price = None
        prefs.max_price = None
        session["prefs"] = _serialize_preferences(prefs)
        _clear_search_state(session)
        logger.info("[chat] price filters cleared")
        messages.append({"type": "text", "content": "Ценовые фильтры сброшены."})
        messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    if action and action.startswith("rooms:"):
        value = action.split(":", 1)[1]
        prefs.rooms = None if value == "any" else [int(value)]
        session["prefs"] = _serialize_preferences(prefs)
        _clear_search_state(session)
        room_text = "Любое количество комнат" if prefs.rooms is None else f"{', '.join(str(r) for r in prefs.rooms)}-комн."
        logger.info("[chat] rooms filter set to %s", prefs.rooms)
        messages.append({"type": "text", "content": f"Установлено: {room_text}."})
        messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    if action == "next":
        logger.info("[chat] navigate next")
        await _move_search_index(request, session, prefs, step=1)
        error = str(session.get("search_error", ""))
        if error:
            messages.append({"type": "text", "content": error, "is_error": True})
        else:
            listing = _get_current_listing(session)
            state = _get_search_state(session)
            if listing and state is not None:
                results = state.get("results", [])
                index = state.get("index", 0)
                exhausted = state.get("exhausted", False)
                messages.append(_make_listing_msg(
                    listing,
                    int(index) + 1 if isinstance(index, int) else 1,
                    len(results) if isinstance(results, list) else 0,
                    isinstance(index, int) and index > 0,
                    isinstance(results, list) and isinstance(index, int)
                    and (index < len(results) - 1 or not bool(exhausted)),
                ))
                _ensure_chat_prefetch(request, session)
        return messages

    if action == "prev":
        logger.info("[chat] navigate prev")
        await _move_search_index(request, session, prefs, step=-1)
        error = str(session.get("search_error", ""))
        if error:
            messages.append({"type": "text", "content": error, "is_error": True})
        else:
            listing = _get_current_listing(session)
            state = _get_search_state(session)
            if listing and state is not None:
                results = state.get("results", [])
                index = state.get("index", 0)
                exhausted = state.get("exhausted", False)
                messages.append(_make_listing_msg(
                    listing,
                    int(index) + 1 if isinstance(index, int) else 1,
                    len(results) if isinstance(results, list) else 0,
                    isinstance(index, int) and index > 0,
                    isinstance(results, list) and isinstance(index, int)
                    and (index < len(results) - 1 or not bool(exhausted)),
                ))
        return messages

    if text:
        pending = session.get("chat_pending_filter")
        if pending:
            value = _parse_int(text)
            if value is None:
                messages.append({"type": "text", "content": "Не удалось распознать число. Введите только сумму в BYN.", "is_error": True})
                return messages
            if pending == "min_price":
                prefs.min_price = value
                logger.info("[chat] min_price set to %d", value)
            else:
                prefs.max_price = value
                logger.info("[chat] max_price set to %d", value)
            session["prefs"] = _serialize_preferences(prefs)
            session["chat_pending_filter"] = None
            _clear_search_state(session)
            city_label = CITY_URLS[prefs.city_key][0]
            messages.append({"type": "text", "content": format_preferences(prefs, city_label)})
            messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
            return messages

        logger.info("[chat] running AI analysis for query=%r", text[:80])
        analysis = await request.app.state.query_analyzer.analyze(text, prefs)

        if not analysis.ai_available:
            logger.warning("[chat] AI unavailable")
            messages.append({"type": "text", "content": "ИИ-поиск временно недоступен. Попробуйте позже или используйте фильтры вручную.", "is_error": True})
            messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
            return messages

        if analysis.intent == "off_topic":
            logger.info("[chat] off_topic query, not searching")
            summary = analysis.summary or "Я помогаю только с поиском квартир в аренду. Напишите, например: «двушка в Минске до 1200»."
            messages.append({"type": "text", "content": summary})
            messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
            return messages

        if not analysis.has_updates():
            logger.info("[chat] AI analysis returned no updates")
            messages.append({"type": "text", "content": "Попробуйте описать запрос подробнее, например: «двушка в Минске до 1200 рядом с метро»."})
            messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
            return messages

        updated_prefs = _apply_query_analysis(prefs, analysis)
        session["prefs"] = _serialize_preferences(updated_prefs)
        session["query_analysis"] = _serialize_query_analysis(analysis)
        _clear_search_state(session)
        logger.info(
            "[chat] AI analysis done: intent=%s city=%s min=%s max=%s rooms=%s",
            analysis.intent, analysis.city_key, analysis.min_price, analysis.max_price, analysis.rooms,
        )

        if analysis.summary:
            messages.append({"type": "text", "content": analysis.summary})

        return await _do_chat_search(request, session, updated_prefs, text, analysis, messages)

    messages.append({"type": "text", "content": "Напишите запрос или используйте кнопки меню."})
    messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
    return messages


async def _do_chat_search(
    request: Request,
    session: dict[str, object],
    prefs: UserPreferences,
    query: str | None,
    analysis: QueryAnalysis | None,
    prefix_messages: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    messages = prefix_messages if prefix_messages is not None else []
    city_label = CITY_URLS[prefs.city_key][0]
    logger.info("[chat] starting search: city=%s min=%s max=%s rooms=%s", prefs.city_key, prefs.min_price, prefs.max_price, prefs.rooms)

    await _perform_search(request, session, prefs)

    error = str(session.get("search_error", ""))
    if error:
        logger.warning("[chat] search error: %s", error)
        messages.append({"type": "text", "content": error, "is_error": True})
        messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    listing = _get_current_listing(session)
    state = _get_search_state(session)
    results = state.get("results", []) if state is not None else []
    index = state.get("index", 0) if state is not None else 0
    exhausted = state.get("exhausted", False) if state is not None else False
    total = len(results) if isinstance(results, list) else 0

    if listing is None:
        messages.append({"type": "text", "content": "По вашим параметрам объявления не найдены. Попробуйте изменить фильтры.", "is_error": True})
        messages.append({"type": "buttons", "buttons": _main_menu_buttons()})
        return messages

    logger.info("[chat] search returned %d results", total)

    messages.append(_make_listing_msg(
        listing,
        int(index) + 1 if isinstance(index, int) else 1,
        total,
        False,
        isinstance(results, list) and isinstance(index, int)
        and (index < len(results) - 1 or not bool(exhausted)),
    ))
    _ensure_chat_prefetch(request, session)
    return messages


def _make_listing_msg(
    listing: Listing,
    position: int,
    total: int,
    can_prev: bool,
    can_next: bool,
) -> dict[str, object]:
    return {
        "type": "listing",
        "listing": {
            "title": listing.title,
            "price_label": listing.price_label,
            "city_label": listing.city_label,
            "address": listing.address,
            "rooms": listing.rooms,
            "area_m2": listing.area_m2,
            "floor": listing.floor,
            "floors_total": listing.floors_total,
            "district": listing.district,
            "metro": listing.metro,
            "contact_name": listing.contact_name,
            "phone_numbers": listing.phone_numbers,
            "description": (listing.description or "")[:3000],
            "url": listing.url,
            "photo_urls": listing.photo_urls[:4],
        },
        "position": position,
        "total": total,
        "can_prev": can_prev,
        "can_next": can_next,
    }


def _city_button_list() -> list[dict[str, str]]:
    return [{"label": label, "action": f"city:{key}"} for key, (label, _) in CITY_URLS.items()]


def _main_menu_buttons() -> list[dict[str, str]]:
    return [
        {"label": "Сменить город", "action": "menu:city"},
        {"label": "Фильтры", "action": "menu:filters"},
        {"label": "Искать", "action": "search"},
        {"label": "Сбросить", "action": "reset"},
    ]


def _filter_buttons(prefs: UserPreferences) -> list[dict[str, str]]:
    buttons = [
        {"label": "Цена от", "action": "filter:min_price"},
        {"label": "Цена до", "action": "filter:max_price"},
        {"label": "Комнаты", "action": "menu:rooms"},
    ]
    if prefs.min_price is not None or prefs.max_price is not None:
        buttons.append({"label": "Сбросить цену", "action": "filter:clear_price"})
    buttons.append({"label": "Назад", "action": "search"})
    return buttons


def _rooms_buttons() -> list[dict[str, str]]:
    return [
        {"label": "Любое", "action": "rooms:any"},
        {"label": "1 комната", "action": "rooms:1"},
        {"label": "2 комнаты", "action": "rooms:2"},
        {"label": "3 комнаты", "action": "rooms:3"},
        {"label": "4+ комнаты", "action": "rooms:4"},
    ]


async def _prefetch_chat_results(app: FastAPI, session_id: str) -> None:
    sessions = app.state.web_sessions
    session = sessions.get(session_id)
    if session is None:
        return
    state = _get_search_state(session)
    if state is None or state.get("prefetch_in_progress") or state.get("exhausted"):
        return
    state["prefetch_in_progress"] = True
    logger.info("[chat] background prefetch started for session=%s", session_id[:8])
    try:
        prefs = _get_session_preferences(session)
        results = state.get("results")
        next_page = state.get("next_page")
        exhausted = state.get("exhausted")
        max_page = state.get("max_page")
        raw_seen_ids = state.get("seen_ids")
        if not isinstance(results, list) or not isinstance(next_page, int) or not isinstance(exhausted, bool):
            return
        if isinstance(max_page, int) and next_page > max_page:
            state["exhausted"] = True
            return
        seen_ids = {str(item) for item in raw_seen_ids} if isinstance(raw_seen_ids, list) else set()
        while True:
            page_result = await app.state.parser.search_page(prefs, page=next_page, seen_ids=seen_ids)
            if isinstance(page_result.max_page, int):
                state["max_page"] = page_result.max_page
            state["next_page"] = next_page + 1
            state["seen_ids"] = sorted(seen_ids)
            if page_result.items:
                results.extend(asdict(item) for item in page_result.items)
                logger.info("[chat] prefetch added %d items for session=%s", len(page_result.items), session_id[:8])
                return
            if not page_result.had_candidates or not page_result.had_unseen_candidates:
                state["exhausted"] = True
                return
            if isinstance(page_result.max_page, int) and next_page >= page_result.max_page:
                state["exhausted"] = True
                return
            next_page += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("[chat] prefetch failed for session=%s: %s", session_id[:8], exc)
    finally:
        refreshed_state = _get_search_state(session)
        if refreshed_state is not None:
            refreshed_state["prefetch_in_progress"] = False
            refreshed_state["prefetch_task"] = None


def _ensure_chat_prefetch(request: Request, session: dict[str, object]) -> None:
    state = _get_search_state(session)
    if state is None:
        return
    results = state.get("results")
    index = state.get("index")
    exhausted = state.get("exhausted")
    prefetch_in_progress = state.get("prefetch_in_progress")
    prefetch_task = state.get("prefetch_task")
    if (
        not isinstance(results, list)
        or not isinstance(index, int)
        or not isinstance(exhausted, bool)
        or not isinstance(prefetch_in_progress, bool)
    ):
        return
    if exhausted or prefetch_in_progress:
        return
    if isinstance(prefetch_task, asyncio.Task) and not prefetch_task.done():
        return
    remaining = len(results) - index - 1
    if remaining > 1:
        return
    session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not session_id:
        return
    logger.info("[chat] scheduling background prefetch (remaining=%d) session=%s", remaining, session_id[:8])
    task = asyncio.create_task(_prefetch_chat_results(request.app, session_id))
    state["prefetch_task"] = task


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Starting web server on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
