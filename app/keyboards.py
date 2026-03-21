from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import CITY_URLS


def city_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for city_key, (label, _) in CITY_URLS.items():
        builder.button(text=label, callback_data=f"city:{city_key}")
    builder.adjust(2)
    return builder.as_markup()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать город", callback_data="menu:city")
    builder.button(text="Настроить фильтры", callback_data="menu:filters")
    builder.button(text="Показать объявления", callback_data="menu:search")
    builder.button(text="Сбросить фильтры", callback_data="menu:reset")
    builder.adjust(2)
    return builder.as_markup()


def filters_keyboard(current_rooms: int | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Мин. цена", callback_data="filter:min_price")
    builder.button(text="Макс. цена", callback_data="filter:max_price")
    builder.button(text=f"Комнаты: {current_rooms or 'любые'}", callback_data="filter:rooms")
    builder.button(text="Сбросить цену", callback_data="filter:clear_price")
    builder.adjust(2)
    return builder.as_markup()


def rooms_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for rooms in range(1, 5):
        builder.button(text=str(rooms), callback_data=f"rooms:{rooms}")
    builder.button(text="Любое количество", callback_data="rooms:any")
    builder.adjust(4, 1)
    return builder.as_markup()
