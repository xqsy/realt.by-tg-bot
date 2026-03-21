from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import CITY_URLS, load_settings
from app.formatters import format_listing_full, format_listing_short, format_preferences, split_message
from app.keyboards import city_keyboard, filters_keyboard, main_menu_keyboard, rooms_keyboard
from app.parser import RealtParser
from app.storage import UserPreferencesRepository

router = Router()


class FilterStates(StatesGroup):
    waiting_for_min_price = State()
    waiting_for_max_price = State()


settings = load_settings()
repository = UserPreferencesRepository(settings.data_dir / "users.sqlite3")
parser = RealtParser(settings)


async def _send_long_message(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.answer(chunk, reply_markup=main_menu_keyboard())


def _city_label(city_key: str) -> str:
    return CITY_URLS[city_key][0]


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    repository.get(message.from_user.id)
    await message.answer(
        "Выберите город для поиска квартир на длительный срок:",
        reply_markup=city_keyboard(),
    )


@router.message(Command("city"))
async def city_command_handler(message: Message) -> None:
    await message.answer("Выберите город:", reply_markup=city_keyboard())


@router.message(Command("filters"))
async def filters_command_handler(message: Message) -> None:
    prefs = repository.get(message.from_user.id)
    await message.answer(
        format_preferences(prefs, _city_label(prefs.city_key)),
        reply_markup=filters_keyboard(prefs.rooms),
    )


@router.message(Command("search"))
async def search_command_handler(message: Message) -> None:
    await _perform_search(message)


@router.message(Command("reset_filters"))
async def reset_command_handler(message: Message) -> None:
    prefs = repository.get(message.from_user.id)
    prefs.min_price = None
    prefs.max_price = None
    prefs.rooms = None
    repository.save(prefs)
    await message.answer("Фильтры сброшены.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("city:"))
async def city_callback_handler(callback: CallbackQuery) -> None:
    city_key = callback.data.split(":", 1)[1]
    prefs = repository.get(callback.from_user.id)
    prefs.city_key = city_key
    repository.save(prefs)
    await callback.message.edit_text(
        f"Город установлен: {_city_label(city_key)}\n\nИспользуйте меню ниже.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:city")
async def menu_city_callback_handler(callback: CallbackQuery) -> None:
    await callback.message.answer("Выберите город:", reply_markup=city_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:filters")
async def menu_filters_callback_handler(callback: CallbackQuery) -> None:
    prefs = repository.get(callback.from_user.id)
    await callback.message.answer(
        format_preferences(prefs, _city_label(prefs.city_key)),
        reply_markup=filters_keyboard(prefs.rooms),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:search")
async def menu_search_callback_handler(callback: CallbackQuery) -> None:
    await callback.answer("Ищу объявления...")
    await _perform_search(callback.message)


@router.callback_query(F.data == "menu:reset")
async def menu_reset_callback_handler(callback: CallbackQuery) -> None:
    prefs = repository.get(callback.from_user.id)
    prefs.min_price = None
    prefs.max_price = None
    prefs.rooms = None
    repository.save(prefs)
    await callback.message.answer("Фильтры сброшены.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "filter:min_price")
async def min_price_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FilterStates.waiting_for_min_price)
    await callback.message.answer("Введите минимальную цену в BYN, например: 500")
    await callback.answer()


@router.callback_query(F.data == "filter:max_price")
async def max_price_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FilterStates.waiting_for_max_price)
    await callback.message.answer("Введите максимальную цену в BYN, например: 1200")
    await callback.answer()


@router.callback_query(F.data == "filter:rooms")
async def rooms_callback_handler(callback: CallbackQuery) -> None:
    await callback.message.answer("Выберите количество комнат:", reply_markup=rooms_keyboard())
    await callback.answer()


@router.callback_query(F.data == "filter:clear_price")
async def clear_price_callback_handler(callback: CallbackQuery) -> None:
    prefs = repository.get(callback.from_user.id)
    prefs.min_price = None
    prefs.max_price = None
    repository.save(prefs)
    await callback.message.answer("Фильтр по цене очищен.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("rooms:"))
async def set_rooms_callback_handler(callback: CallbackQuery) -> None:
    value = callback.data.split(":", 1)[1]
    prefs = repository.get(callback.from_user.id)
    prefs.rooms = None if value == "any" else int(value)
    repository.save(prefs)
    await callback.message.answer("Фильтр по комнатам обновлён.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(FilterStates.waiting_for_min_price)
async def min_price_message_handler(message: Message, state: FSMContext) -> None:
    value = _parse_price_input(message.text or "")
    if value is None:
        await message.answer("Не удалось распознать число. Введите только сумму в BYN.")
        return
    prefs = repository.get(message.from_user.id)
    prefs.min_price = value
    repository.save(prefs)
    await state.clear()
    await message.answer("Минимальная цена сохранена.", reply_markup=main_menu_keyboard())


@router.message(FilterStates.waiting_for_max_price)
async def max_price_message_handler(message: Message, state: FSMContext) -> None:
    value = _parse_price_input(message.text or "")
    if value is None:
        await message.answer("Не удалось распознать число. Введите только сумму в BYN.")
        return
    prefs = repository.get(message.from_user.id)
    prefs.max_price = value
    repository.save(prefs)
    await state.clear()
    await message.answer("Максимальная цена сохранена.", reply_markup=main_menu_keyboard())


@router.message()
async def fallback_handler(message: Message) -> None:
    if not message.text:
        await message.answer("Используйте кнопки меню или команды /start, /city, /filters, /search.")
        return
    lowered = message.text.lower().strip()
    if lowered in {"город", "сменить город"}:
        await city_command_handler(message)
        return
    if lowered in {"фильтры", "настроить фильтры"}:
        await filters_command_handler(message)
        return
    if lowered in {"поиск", "показать объявления"}:
        await _perform_search(message)
        return
    await message.answer("Используйте кнопки меню или команды /start, /city, /filters, /search.")


async def _perform_search(message: Message) -> None:
    prefs = repository.get(message.from_user.id)
    city_label = _city_label(prefs.city_key)
    waiting_message = await message.answer(f"Ищу объявления: {city_label}...")
    try:
        result = await parser.search(prefs, limit=5)
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
    with suppress(TelegramBadRequest):
        await waiting_message.delete()
    summary = [
        f"Найдено {len(result.items)} объявлений. Город: {city_label}",
        format_preferences(prefs, city_label),
        "",
    ]
    for index, item in enumerate(result.items, start=1):
        summary.append(format_listing_short(index, item))
        summary.append("")
    await _send_long_message(message, "\n".join(summary).strip())
    for item in result.items:
        await _send_long_message(message, format_listing_full(item))


def _parse_price_input(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


async def _main() -> None:
    if not settings.bot_token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await parser.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(_main())
