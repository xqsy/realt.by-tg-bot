from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import CITY_URLS


def city_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for city_key, (label, _) in CITY_URLS.items():
        current_row.append(InlineKeyboardButton(text=label, callback_data=f"city:{city_key}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text="Выбрать город", callback_data="menu:city"),
                InlineKeyboardButton(text="Настроить фильтры", callback_data="menu:filters"),
            ],
            [
                InlineKeyboardButton(text="Сбросить фильтры", callback_data="menu:reset"),
            ],
            [
                InlineKeyboardButton(text="Показать объявления", callback_data="menu:search"),
            ],
        ]
    )


def filters_keyboard(current_rooms: int | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text="Мин. цена", callback_data="filter:min_price"),
                InlineKeyboardButton(text="Макс. цена", callback_data="filter:max_price"),
            ],
            [
                InlineKeyboardButton(text=f"Комнаты: {current_rooms or 'любые'}", callback_data="filter:rooms"),
                InlineKeyboardButton(text="Сбросить цену", callback_data="filter:clear_price"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="filter:back"),
            ],
        ]
    )


def rooms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text="1", callback_data="rooms:1"),
                InlineKeyboardButton(text="2", callback_data="rooms:2"),
                InlineKeyboardButton(text="3", callback_data="rooms:3"),
                InlineKeyboardButton(text="4", callback_data="rooms:4"),
            ],
            [InlineKeyboardButton(text="Любое количество", callback_data="rooms:any")],
        ]
    )


def search_navigation_keyboard(has_previous: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    navigation_row: list[InlineKeyboardButton] = []
    if has_previous:
        navigation_row.append(InlineKeyboardButton(text="Предыдущее", callback_data="search:prev"))
    if has_next:
        navigation_row.append(InlineKeyboardButton(text="Следующее", callback_data="search:next"))
    if navigation_row:
        rows.append(navigation_row)
    rows.append(
        [
            InlineKeyboardButton(text="Фильтры", callback_data="menu:filters"),
            InlineKeyboardButton(text="В меню", callback_data="search:menu"),
        ]
    )
    return InlineKeyboardMarkup(rows)
