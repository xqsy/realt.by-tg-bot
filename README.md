# Realt Bot

Telegram-бот для поиска квартир в долгосрочную аренду с realt.by. (Так же есть возможность запустить веб-версию.)

Бот умеет:
- выбирать город поиска
- фильтровать объявления по цене в BYN
- фильтровать по количеству комнат
- искать через обычное меню или по текстовому запросу
- использовать ИИ для разбора свободного текста
- показывать объявления с навигацией в чате

## Требования

- Python 3.11+
- Telegram Bot Token

## Как создать бота через BotFather

1. Откройте в Telegram бота `@BotFather`.
2. Отправьте команду `/newbot`.
3. Укажите имя бота, которое будет видно пользователям.
4. Укажите username бота, который должен заканчиваться на `bot`, например `realt_rent_helper_bot`.
5. BotFather отправит токен вида `123456:ABC-DEF...`.
6. Скопируйте этот токен в файл `.env` в переменную `BOT_TOKEN`.

Пример:
```env
BOT_TOKEN=your_telegram_bot_token_here
```

## Установка

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Настройка

Пример переменных окружения находится в файле `.env.example`.

Основные параметры:

- `BOT_TOKEN` — токен Telegram-бота
- `REQUEST_TIMEOUT` — таймаут HTTP-запросов в секундах
- `DATA_DIR` — папка для локальных данных, включая SQLite-базу пользователей

Параметры для ИИ-разбора запросов:

- `AI_API_KEY` — API-ключ провайдера
- `AI_BASE_URL` — базовый URL API
- `AI_MODEL` — модель для разбора пользовательских запросов
- `AI_ENABLE_REASONING` — включает reasoning, если провайдер и модель это поддерживают

## Пример `.env`

```env
BOT_TOKEN=your_telegram_bot_token_here
REQUEST_TIMEOUT=20
DATA_DIR=data
AI_API_KEY=
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemma-3-4b-it:free
AI_ENABLE_REASONING=false
```

## Запуск

После настройки окружения выполните:

```bash
python main.py
```

Для запуска сайта выполните:

```bash
python web_main.py
```

## Как пользоваться

После запуска бота можно:

- выбрать город через кнопки
- настроить фильтры вручную
- нажать `Показать объявления`
- написать запрос в свободной форме, например:

```text
двушка в Минске до 1200 рядом с метро
3 комнаты в Гомеле до 900
однушка в Бресте недорого
```

## Структура проекта

```text
bot/
  app.py         # логика Telegram-бота
  keyboards.py   # inline-клавиатуры Telegram
web/
  app.py         # логика сайта на FastAPI
  templates/
    chat.html    # шаблон страницы сайта
core/
  parser.py      # парсинг realt.by
  ai.py          # разбор текстовых запросов и ранжирование
  config.py      # настройки и города
  formatters.py  # форматирование сообщений
  models.py      # dataclass-модели
  storage.py     # хранение пользовательских фильтров
main.py          # точка входа Telegram-бота
web_main.py      # точка входа сайта
requirements.txt
.env.example
```
