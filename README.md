# Market Goods Bot

Бот для продавца маркетплейсов: Ozon, Яндекс Маркет, сайт на Тильде, а также доставок через Ozon, СДЭК и Яндекс Доставку. Работает в двух мессенджерах: **Telegram** и **MAX** — общий код мониторинга, разные транспортные модули (`bot.py` для Telegram, `max_bot.py` для MAX).

Бот отслеживает цены, остатки FBO/FBS, возвраты, отзывы, заказы, товары и заказы сайта, доставки, показывает отчёты отдельно для источников и отправляет уведомления при важных изменениях.

Для Ozon в ценовых отчётах выводятся три доступных поля: «С Ozon-акциями», «Ozon-индекс», «Конкуренты».

## Возможности

- Ozon: товары, цены, остатки с учётом резерва, возвраты, отзывы, заказы, доставка.
- Яндекс Маркет: офферы, цены, статусы, остатки FIT, заказы, отзывы (если API возвращает данные).
- Тильда: только включённые в продажу товары и цены (6 позиций из 17), заказы сайта за 15 или 30 дней, push о новых оплаченных заказах. Интеграция только на чтение: бот не меняет статусы и не редактирует товары.
- Доставка: отчёты по Ozon, Яндекс Доставке и СДЭК.
- Раздельные включения/выключения мониторинга магазинов и функций.
- Push-уведомления: изменение цен, низкие остатки, новые заказы, доставка, ПВЗ и срок хранения.
- Ежедневный отчёт.
- Настройки через inline-кнопки.

## Telegram и MAX

| | Telegram (`bot.py`) | MAX (`max_bot.py`) |
|---|---|---|
| Транспорт | long polling (python-telegram-bot) | long polling `GET /updates` (MAX Bot API, `platform-api2.max.ru`) |
| Токен | `TELEGRAM_BOT_TOKEN` | `MAX_BOT_TOKEN` (из business.max.ru) |
| Доступ | первый `/start` становится владельцем (`ADMIN_CHAT_ID` — заранее) | приватно: «Поделиться контактом», номер в `MAX_ADMIN_PHONES` (запасной вход — `MAX_ADMIN_USER_IDS`) |
| Состояние | `DATA_DIR` (по умолчанию `data`) | `MAX_DATA_DIR` (по умолчанию `data_max`) |
| Сервис (пример) | `ozon_goods_bot.service` | `max_goods_bot.service` |

Оба бота могут работать параллельно на одном сервере: бизнес-логика, клиенты API и отчёты общие, а состояния разделены. Команды и inline-меню одинаковы в обоих мессенджерах.

## Требования

- Python 3.11+
- Ozon Seller API credentials
- Telegram bot token и/или MAX bot token
- опционально: токены Яндекс Доставки, СДЭК, Яндекс Маркета, MCP-токен Тильды

## Установка

1. Создать виртуальное окружение и установить зависимости:

```bash
python -m venv venv
pip install -r requirements.txt
```

2. Скопировать `.env.example` в `.env` и заполнить токены.

   Секреты не должны попадать в репозиторий.

3. Запустить:

```bash
python bot.py
```

## Развёртывание на сервере (Linux)

Рабочая копия: `/root/ozon_goods_bot`. Сервисы systemd: `ozon_goods_bot.service` (Telegram), `max_goods_bot.service` (MAX).

1. Обновите файлы в `/root/ozon_goods_bot`. Файл `.env` не перезаписывается деплоем.
2. Проверьте синтаксис:

```bash
python3 -m py_compile bot.py max_bot.py tilda.py tilda_site.py shipping.py
```

3. Перезапустите нужные сервисы и проверьте статус:

```bash
systemctl restart ozon_goods_bot.service   # Telegram
systemctl restart max_goods_bot.service    # MAX
systemctl is-active ozon_goods_bot.service max_goods_bot.service
```

4. Логи:

```bash
journalctl -u ozon_goods_bot.service -n 50 --no-pager
journalctl -u max_goods_bot.service -n 50 --no-pager
```

Секреты хранятся только в `/root/ozon_goods_bot/.env` (права `600`). После изменения `.env` перезапустите сервисы.

## Основные переменные `.env`

- `TELEGRAM_BOT_TOKEN`
- `MAX_BOT_TOKEN` — токен бота MAX (business.max.ru → Чат-боты → Настройки)
- `MAX_ADMIN_PHONES` — номера владельца через запятую (вход в MAX по «Поделиться контактом»)
- `MAX_ADMIN_USER_IDS` — запасной вход в MAX по user_id (можно оставить пустым)
- `MAX_DATA_DIR` — каталог состояния MAX-бота (по умолчанию `data_max`)
- `OZON_CLIENT_ID`
- `OZON_API_KEY`
- `YANDEX_MARKET_API_KEY`
- `YANDEX_DELIVERY_TOKEN`
- `CDEK_ACCOUNT`, `CDEK_SECURE_PASSWORD`
- `ADMIN_CHAT_ID`
- `DELIVERY_STORAGE_WARN_HOURS`
- `TILDA_PROJECT_ID` — ID проекта на Тильде
- `TILDA_MCP_TOKEN` — токен MCP Тильды (только в `.env`, не в репозитории)
- `TILDA_MCP_URL` — адрес MCP-сервера, по умолчанию `https://tilda.ru/api/mcp/`

## Команды

Одинаковые в Telegram и MAX:

- `/start` — регистрация владельца (в MAX — подтверждение по контакту)
- `/help`
- `/sync` — забрать товары из Ozon и обновить цены/остатки (в отчёте также Тильда и Яндекс Маркет)
- `/check` — проверить цены, остатки, возвраты, доставки и Тильду сейчас
- `/list [N]`, `/stock [N]`, `/returns [N]`
- `/prices`, `/index`, `/orders`
- `/deliveries` — отчёты по доставкам (Ozon, Яндекс Доставка, СДЭК, Яндекс Маркет)
- `/reviews`, `/returnstats`, `/ym`
- `/tilda [N]` — товары в продаже и заказы сайта Тильды
- `/settings` — все настройки (окно заказов 1–30 дн общее для Ozon/ЯМ/Тильды)

## Безопасность

Репозиторий содержит только шаблон конфигурации. Секреты, `.env`, state, логи, дампы и кэш исключаются из Git через `.gitignore`.

## Лицензия

MIT License. Copyright (c) 2026 akdengi.
