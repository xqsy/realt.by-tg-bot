from __future__ import annotations

import asyncio
import logging
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import CITY_URLS, load_settings
from app.formatters import format_listing_full, format_preferences, split_message
from app.keyboards import city_keyboard, filters_keyboard, main_menu_keyboard, rooms_keyboard, search_navigation_keyboard
from app.parser import RealtParser
from app.storage import UserPreferencesRepository

settings = load_settings()
repository = UserPreferencesRepository(settings.data_dir / "users.sqlite3")
parser = RealtParser(settings)
SEARCH_FETCH_LIMIT = 20


async def _send_long_message(target_message, text: str, reply_markup=None) -> None:
    for chunk in split_message(text):
        await target_message.reply_text(chunk, reply_markup=reply_markup or main_menu_keyboard())


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
    if data == "search:menu":
        await query.message.reply_text("Возврат в меню.", reply_markup=main_menu_keyboard())
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
    context.user_data["search_index"] = 0
    await _send_search_item(message, context, prefs)


async def _show_search_item(update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    results = context.user_data.get("search_results")
    if not isinstance(results, list) or not results:
        await query.message.reply_text("Сначала выполните поиск объявлений.", reply_markup=main_menu_keyboard())
        return
    current_index = context.user_data.get("search_index", 0)
    if not isinstance(current_index, int):
        current_index = 0
    new_index = current_index + step
    if new_index < 0 or new_index >= len(results):
        await query.answer("Больше объявлений нет.")
        return
    context.user_data["search_index"] = new_index
    if update.effective_user is None:
        await query.message.reply_text("Не удалось получить параметры поиска.", reply_markup=main_menu_keyboard())
        return
    prefs = repository.get(update.effective_user.id)
    await _send_search_item(query.message, context, prefs)


async def _send_search_item(target_message, context: ContextTypes.DEFAULT_TYPE, prefs) -> None:
    results = context.user_data.get("search_results")
    city_label = context.user_data.get("search_city_label")
    index = context.user_data.get("search_index", 0)
    if not isinstance(results, list) or not isinstance(city_label, str) or not isinstance(index, int):
        await target_message.reply_text("Не удалось подготовить результаты поиска.", reply_markup=main_menu_keyboard())
        return
    if index < 0 or index >= len(results):
        await target_message.reply_text("Больше объявлений нет.", reply_markup=main_menu_keyboard())
        return
    item = results[index]
    message_lines = [
        f"Объявление {index + 1} из {len(results)}",
        f"Город: {city_label}",
        "",
        format_preferences(prefs, city_label),
        "",
        format_listing_full(item),
    ]
    await _send_long_message(
        target_message,
        "\n".join(message_lines).strip(),
        reply_markup=search_navigation_keyboard(index > 0, index < len(results) - 1),
    )


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
