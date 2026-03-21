from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.ai import HousingQueryAnalyzer, QueryAnalysis
from app.config import CITY_URLS, load_settings
from app.formatters import format_listing_full, format_preferences, split_message
from app.keyboards import city_keyboard, filters_keyboard, main_menu_keyboard, rooms_keyboard, search_navigation_keyboard
from app.models import UserPreferences
from app.parser import RealtParser
from app.storage import UserPreferencesRepository

settings = load_settings()
repository = UserPreferencesRepository(settings.data_dir / "users.sqlite3")
parser = RealtParser(settings)
query_analyzer = HousingQueryAnalyzer(settings)
SEARCH_STATE_KEY = "search_state"


async def _send_long_message(target_message, text: str, reply_markup=None) -> None:
    for chunk in split_message(text):
        await target_message.reply_text(chunk, reply_markup=reply_markup or main_menu_keyboard())


def _city_label(city_key: str) -> str:
    return CITY_URLS[city_key][0]


def _clear_search_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(SEARCH_STATE_KEY, None)
    context.user_data.pop("query_analysis", None)


def _create_search_state(city_label: str, city_key: str) -> dict[str, object]:
    return {
        "results": [],
        "city_label": city_label,
        "city_key": city_key,
        "index": 0,
        "next_page": 1,
        "seen_ids": set(),
        "exhausted": False,
    }


def _get_search_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object] | None:
    state = context.user_data.get(SEARCH_STATE_KEY)
    return state if isinstance(state, dict) else None


async def _load_next_search_page(context: ContextTypes.DEFAULT_TYPE, prefs) -> bool:
    state = _get_search_state(context)
    if state is None:
        return False
    results = state.get("results")
    seen_ids = state.get("seen_ids")
    next_page = state.get("next_page")
    exhausted = state.get("exhausted")
    if not isinstance(results, list) or not isinstance(seen_ids, set) or not isinstance(next_page, int) or not isinstance(exhausted, bool):
        return False
    if exhausted:
        return False
    while True:
        page_result = await parser.search_page(prefs, page=next_page, seen_ids=seen_ids)
        state["next_page"] = next_page + 1
        if page_result.items:
            results.extend(page_result.items)
            return True
        if not page_result.had_candidates:
            state["exhausted"] = True
            return False
        next_page += 1


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    repository.get(update.effective_user.id)
    await update.message.reply_text(
        "Я помогу подобрать квартиру в долгосрочную аренду.\n\n"
        "Вы можете работать двумя способами:\n"
        "- написать запрос в свободной форме, например: двушка в Минске до 1200 рядом с метро\n"
        "- либо выбрать город и настроить фильтры вручную через кнопки\n\n"
        "Сначала выберите город для поиска:",
        reply_markup=city_keyboard(),
    )


async def city_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("Выберите город:", reply_markup=city_keyboard())


async def filters_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    prefs = repository.get(update.effective_user.id)
    await update.message.reply_text(
        format_preferences(prefs, _city_label(prefs.city_key)),
        reply_markup=filters_keyboard(prefs.rooms),
    )


async def search_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _perform_search(update, context)


async def reset_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    prefs = repository.get(update.effective_user.id)
    prefs.min_price = None
    prefs.max_price = None
    prefs.rooms = None
    repository.save(prefs)
    await update.message.reply_text(
        format_preferences(prefs, _city_label(prefs.city_key)),
        reply_markup=main_menu_keyboard(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None or query.message is None or query.data is None:
        return
    await query.answer()
    data = query.data
    prefs = repository.get(update.effective_user.id)
    if data.startswith("city:"):
        city_key = data.split(":", 1)[1]
        prefs.city_key = city_key
        repository.save(prefs)
        _clear_search_state(context)
        await query.edit_message_text(
            format_preferences(prefs, _city_label(city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "menu:city":
        await query.message.reply_text("Выберите город:", reply_markup=city_keyboard())
        return
    if data == "menu:filters":
        await query.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=filters_keyboard(prefs.rooms),
        )
        return
    if data == "menu:search":
        await _perform_search(update, context)
        return
    if data == "menu:reset":
        prefs.min_price = None
        prefs.max_price = None
        prefs.rooms = None
        repository.save(prefs)
        _clear_search_state(context)
        await query.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "search:menu":
        await query.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "search:next":
        await _show_search_item(update, context, step=1)
        return
    if data == "search:prev":
        await _show_search_item(update, context, step=-1)
        return
    if data == "filter:min_price":
        context.user_data["pending_filter"] = "min_price"
        await query.message.reply_text("Введите минимальную цену в BYN, например: 500")
        return
    if data == "filter:max_price":
        context.user_data["pending_filter"] = "max_price"
        await query.message.reply_text("Введите максимальную цену в BYN, например: 1200")
        return
    if data == "filter:rooms":
        await query.message.reply_text("Выберите количество комнат:", reply_markup=rooms_keyboard())
        return
    if data == "filter:clear_price":
        prefs.min_price = None
        prefs.max_price = None
        repository.save(prefs)
        _clear_search_state(context)
        await query.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return
    if data == "filter:back":
        try:
            await query.message.delete()
        except BadRequest:
            pass
        return
    if data.startswith("rooms:"):
        value = data.split(":", 1)[1]
        prefs.rooms = None if value == "any" else int(value)
        repository.save(prefs)
        _clear_search_state(context)
        await query.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None or not update.message.text:
        if update.message is not None:
            await update.message.reply_text("Используйте кнопки меню или команды /start, /city, /filters, /search.")
        return
    pending_filter = context.user_data.get("pending_filter")
    if pending_filter is not None:
        value = _parse_price_input(update.message.text)
        if value is None:
            await update.message.reply_text("Не удалось распознать число. Введите только сумму в BYN.")
            return
        prefs = repository.get(update.effective_user.id)
        if pending_filter == "min_price":
            prefs.min_price = value
        else:
            prefs.max_price = value
        repository.save(prefs)
        _clear_search_state(context)
        context.user_data.pop("pending_filter", None)
        await update.message.reply_text(
            format_preferences(prefs, _city_label(prefs.city_key)),
            reply_markup=main_menu_keyboard(),
        )
        return
    lowered = update.message.text.lower().strip()
    if lowered in {"город", "сменить город"}:
        await city_command_handler(update, context)
        return
    if lowered in {"фильтры", "настроить фильтры"}:
        await filters_command_handler(update, context)
        return
    if lowered in {"поиск", "показать объявления"}:
        await _perform_search(update, context)
        return
    analysis_wait_message = await update.message.reply_text("Анализирую запрос через ИИ...")
    prefs = repository.get(update.effective_user.id)
    analysis = await query_analyzer.analyze(update.message.text, prefs)
    try:
        await analysis_wait_message.delete()
    except BadRequest:
        pass
    if not analysis.ai_available:
        await update.message.reply_text("ИИ-поиск временно недоступен. Попробуйте позже или используйте фильтры вручную.", reply_markup=main_menu_keyboard())
        return
    if not analysis.has_updates():
        await update.message.reply_text(
            "Используйте кнопки меню или команды /start, /city, /filters, /search. Также можно написать запрос в свободной форме, например: двушка в Минске до 1200 рядом с метро.",
            reply_markup=main_menu_keyboard(),
        )
        return
    updated_prefs = _apply_query_analysis(prefs, analysis)
    repository.save(updated_prefs)
    _clear_search_state(context)
    context.user_data["query_analysis"] = analysis
    answer = _format_analysis_result(updated_prefs, analysis)
    await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
    await _perform_search(update, context)


async def _perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    prefs = repository.get(user.id)
    analysis = _get_query_analysis(context)
    city_label = _city_label(prefs.city_key)
    waiting_message = await message.reply_text(f"Ищу объявления: {city_label}...")
    context.user_data[SEARCH_STATE_KEY] = _create_search_state(city_label, prefs.city_key)
    try:
        loaded = await _load_next_search_page(context, prefs)
    except Exception as exc:
        logging.exception("Search failed", exc_info=exc)
        await waiting_message.edit_text("Не удалось получить объявления с realt.by. Попробуйте позже.")
        return
    state = _get_search_state(context)
    results = state.get("results") if state is not None else None
    if not loaded or not isinstance(results, list) or not results:
        await waiting_message.edit_text(
            "По вашим параметрам объявления не найдены. Попробуйте сменить город или ослабить фильтры.",
            reply_markup=main_menu_keyboard(),
        )
        _clear_search_state(context)
        return
    if analysis is not None:
        ranked_results = query_analyzer.rank_listings(results, analysis, prefs)
        state["results"] = ranked_results
    try:
        await waiting_message.delete()
    except BadRequest:
        pass
    if analysis is not None and analysis.summary:
        await message.reply_text(analysis.summary, reply_markup=main_menu_keyboard())
    await _send_search_item(message, context)


async def _show_search_item(update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    state = _get_search_state(context)
    if state is None:
        await query.message.reply_text("Сначала выполните поиск объявлений.", reply_markup=main_menu_keyboard())
        return
    results = state.get("results")
    current_index = state.get("index", 0)
    exhausted = state.get("exhausted", False)
    city_key = state.get("city_key")
    if not isinstance(results, list) or not results or not isinstance(exhausted, bool) or not isinstance(city_key, str):
        await query.message.reply_text("Сначала выполните поиск объявлений.", reply_markup=main_menu_keyboard())
        return
    if not isinstance(current_index, int):
        current_index = 0
    new_index = current_index + step
    if new_index < 0:
        await query.answer("Это первое объявление.")
        return
    if new_index >= len(results):
        if exhausted:
            await query.answer("Больше объявлений нет.")
            await query.message.reply_text(
                "Больше квартир по таким фильтрам не найдено.",
                reply_markup=main_menu_keyboard(),
            )
            return
        prefs = repository.get(update.effective_user.id)
        if prefs.city_key != city_key:
            prefs.city_key = city_key
        try:
            loaded = await _load_next_search_page(context, prefs)
        except Exception as exc:
            logging.exception("Failed to load next search page", exc_info=exc)
            await query.message.reply_text("Не удалось загрузить следующую страницу объявлений.", reply_markup=main_menu_keyboard())
            return
        state = _get_search_state(context)
        results = state.get("results") if state is not None else None
        analysis = _get_query_analysis(context)
        if analysis is not None and state is not None and isinstance(results, list):
            ranked_results = query_analyzer.rank_listings(results, analysis, prefs)
            state["results"] = ranked_results
            results = ranked_results
        if not loaded or not isinstance(results, list) or new_index >= len(results):
            await query.answer("Больше объявлений нет.")
            await query.message.reply_text(
                "Больше квартир по таким фильтрам не найдено.",
                reply_markup=main_menu_keyboard(),
            )
            return
    state["index"] = new_index
    await _send_search_item(query.message, context)


async def _send_search_item(target_message, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _get_search_state(context)
    if state is None:
        await target_message.reply_text("Не удалось подготовить результаты поиска.", reply_markup=main_menu_keyboard())
        return
    results = state.get("results")
    index = state.get("index", 0)
    exhausted = state.get("exhausted", False)
    if not isinstance(results, list) or not isinstance(index, int) or not isinstance(exhausted, bool):
        await target_message.reply_text("Не удалось подготовить результаты поиска.", reply_markup=main_menu_keyboard())
        return
    if index < 0 or index >= len(results):
        await target_message.reply_text("Больше объявлений нет.", reply_markup=main_menu_keyboard())
        return
    item = results[index]
    await _send_long_message(
        target_message,
        format_listing_full(item),
        reply_markup=search_navigation_keyboard(index > 0, index < len(results) - 1 or not exhausted),
    )


def _parse_price_input(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def _get_query_analysis(context: ContextTypes.DEFAULT_TYPE) -> QueryAnalysis | None:
    analysis = context.user_data.get("query_analysis")
    return analysis if isinstance(analysis, QueryAnalysis) else None


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
        updated.rooms = analysis.rooms
    return updated


def _format_analysis_result(prefs: UserPreferences, analysis: QueryAnalysis) -> str:
    lines = ["Запрос распознан."]
    if analysis.intent == "refine":
        lines.append("Режим: уточнение текущего поиска")
    else:
        lines.append("Режим: новый поиск")
    lines.append(f"Город: {_city_label(prefs.city_key)}")
    lines.append(f"Цена от: {prefs.min_price if prefs.min_price is not None else 'не задана'}")
    lines.append(f"Цена до: {prefs.max_price if prefs.max_price is not None else 'не задана'}")
    lines.append(f"Комнаты: {prefs.rooms if prefs.rooms is not None else 'любое количество'}")
    if analysis.features:
        lines.append("Пожелания: " + ", ".join(analysis.features))
    return "\n".join(lines)


def run() -> None:
    if not settings.bot_token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    logging.basicConfig(level=logging.INFO)
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = ApplicationBuilder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("city", city_command_handler))
    application.add_handler(CommandHandler("filters", filters_command_handler))
    application.add_handler(CommandHandler("search", search_command_handler))
    application.add_handler(CommandHandler("reset_filters", reset_command_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.run_polling()
