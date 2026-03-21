from __future__ import annotations

import asyncio
import logging
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import CITY_URLS, load_settings
from app.formatters import format_listing_full, format_listing_short, format_preferences, split_message
from app.keyboards import city_keyboard, filters_keyboard, main_menu_keyboard, rooms_keyboard, search_pagination_keyboard
from app.parser import RealtParser
from app.storage import UserPreferencesRepository

settings = load_settings()
repository = UserPreferencesRepository(settings.data_dir / "users.sqlite3")
parser = RealtParser(settings)
USER_PAGE_SIZE = 5
SEARCH_FETCH_LIMIT = 20


async def _send_long_message(target_message, text: str) -> None:
    for chunk in split_message(text):
        await target_message.reply_text(chunk, reply_markup=main_menu_keyboard())


def _city_label(city_key: str) -> str:
    return CITY_URLS[city_key][0]


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    repository.get(update.effective_user.id)
    await update.message.reply_text(
        "Выберите город для поиска квартир на длительный срок:",
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
    await update.message.reply_text("Фильтры сброшены.", reply_markup=main_menu_keyboard())


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
        await query.edit_message_text(
            f"Город установлен: {_city_label(city_key)}\n\nИспользуйте меню ниже.",
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
        await query.message.reply_text("Фильтры сброшены.", reply_markup=main_menu_keyboard())
        return
    if data == "search:next":
        await _show_next_page(update, context)
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
        await query.message.reply_text("Фильтр по цене очищен.", reply_markup=main_menu_keyboard())
        return
    if data.startswith("rooms:"):
        value = data.split(":", 1)[1]
        prefs.rooms = None if value == "any" else int(value)
        repository.save(prefs)
        await query.message.reply_text("Фильтр по комнатам обновлён.", reply_markup=main_menu_keyboard())
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
            answer = "Минимальная цена сохранена."
        else:
            prefs.max_price = value
            answer = "Максимальная цена сохранена."
        repository.save(prefs)
        context.user_data.pop("pending_filter", None)
        await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
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
    await update.message.reply_text("Используйте кнопки меню или команды /start, /city, /filters, /search.")


async def _perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    prefs = repository.get(user.id)
    city_label = _city_label(prefs.city_key)
    waiting_message = await message.reply_text(f"Ищу объявления: {city_label}...")
    try:
        result = await parser.search(prefs, limit=SEARCH_FETCH_LIMIT)
    except Exception as exc:
        logging.exception("Search failed", exc_info=exc)
        await waiting_message.edit_text("Не удалось получить объявления с realt.by. Попробуйте позже.")
        return
    if not result.items:
        await waiting_message.edit_text(
            "По вашим параметрам объявления не найдены. Попробуйте сменить город или ослабить фильтры.",
            reply_markup=main_menu_keyboard(),
        )
        return
    try:
        await waiting_message.delete()
    except BadRequest:
        pass
    context.user_data["search_results"] = result.items
    context.user_data["search_city_label"] = city_label
    context.user_data["search_page"] = 0
    await _send_search_page(message, context, prefs)


async def _show_next_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    results = context.user_data.get("search_results")
    if not isinstance(results, list) or not results:
        await query.message.reply_text("Сначала выполните поиск объявлений.", reply_markup=main_menu_keyboard())
        return
    current_page = context.user_data.get("search_page", 0)
    if not isinstance(current_page, int):
        current_page = 0
    next_page = current_page + 1
    if next_page * USER_PAGE_SIZE >= len(results):
        await query.message.reply_text("Больше объявлений нет.", reply_markup=main_menu_keyboard())
        return
    context.user_data["search_page"] = next_page
    if update.effective_user is None:
        await query.message.reply_text("Не удалось получить параметры поиска.", reply_markup=main_menu_keyboard())
        return
    prefs = repository.get(update.effective_user.id)
    await _send_search_page(query.message, context, prefs)


async def _send_search_page(target_message, context: ContextTypes.DEFAULT_TYPE, prefs) -> None:
    results = context.user_data.get("search_results")
    city_label = context.user_data.get("search_city_label")
    page = context.user_data.get("search_page", 0)
    if not isinstance(results, list) or not isinstance(city_label, str) or not isinstance(page, int):
        await target_message.reply_text("Не удалось подготовить результаты поиска.", reply_markup=main_menu_keyboard())
        return
    start = page * USER_PAGE_SIZE
    end = start + USER_PAGE_SIZE
    items = results[start:end]
    if not items:
        await target_message.reply_text("Больше объявлений нет.", reply_markup=main_menu_keyboard())
        return
    total_pages = (len(results) + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE
    summary = [
        f"Найдено объявлений: {len(results)}",
        f"Город: {city_label}",
        f"Страница: {page + 1} из {total_pages}",
        "",
        format_preferences(prefs, city_label),
        "",
    ]
    for index, item in enumerate(items, start=start + 1):
        summary.append(format_listing_short(index, item))
        summary.append("")
    await target_message.reply_text(
        "\n".join(summary).strip(),
        reply_markup=search_pagination_keyboard(end < len(results)) or main_menu_keyboard(),
    )
    for item in items:
        await _send_long_message(target_message, format_listing_full(item))


def _parse_price_input(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


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
