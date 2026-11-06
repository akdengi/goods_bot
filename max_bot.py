"""MAX-версия бота поверх общего движка bot.py (только чтение Тильды, как и в TG).

Транспорт: MAX Bot API (platform-api2.max.ru), long polling GET /updates.
Приватность: доступ только владельцу — при старте бот просит «Поделиться
контактом» и сверяет номер со списком MAX_ADMIN_PHONES из .env
(запасной вариант — MAX_ADMIN_USER_IDS). Все данные — в .env на сервере.

Запуск: python3 max_bot.py  (systemd-юнит max_goods_bot.service).
Состояние: отдельный каталог data_max, чтобы не смешивать с Telegram-ботом.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").strip().rstrip("/")
MAX_ADMIN_PHONES = [p for p in re.split(r"[,;]+", os.getenv("MAX_ADMIN_PHONES", "").strip()) if p.strip()]
MAX_ADMIN_USER_IDS = [
    int(x) for x in re.split(r"[,;\s]+", os.getenv("MAX_ADMIN_USER_IDS", "").strip()) if x.strip().isdigit()
]
# Своё хранилище состояния для MAX-бота (bot.py читает DATA_DIR при импорте).
os.environ["DATA_DIR"] = os.getenv("MAX_DATA_DIR", "").strip() or str(BASE_DIR / "data_max")

import httpx  # noqa: E402

import bot  # noqa: E402  # общий движок: клиенты, команды, periodic-циклы
import tilda_site  # noqa: E402  # noqa нужен для re-export в тестах

# ADMIN_CHAT_ID из .env — это Telegram-чат; в MAX-состоянии ему делать нечего.
bot.ADMIN_CHAT_ID_RAW = ""

logger = logging.getLogger("max_goods_bot")

MAX_TEXT_LIMIT = 4000
POLL_TYPES = "message_created,message_callback,bot_started"

COMMANDS: List[tuple] = [
    ("start", "Главное меню и статус"),
    ("help", "Справка по командам"),
    ("sync", "Загрузить товары из Ozon"),
    ("check", "Проверить цены/остатки сейчас"),
    ("stock", "Остатки Ozon/ЯМ/Тильда"),
    ("orders", "Последние заказы"),
    ("deliveries", "Доставки Ozon/Яндекс/СДЭК"),
    ("report", "Ежедневный отчёт сейчас"),
    ("prices", "Цены Ozon"),
    ("tilda", "Тильда: цены и заказы"),
    ("settings", "Настройки"),
]


# ---------------------------------------------------------------------------
# MAX API client
# ---------------------------------------------------------------------------

class MaxClient:
    def __init__(self, token: str):
        self.token = token
        # На некоторых машинах (например, локальный Windows) нет доверия
        # к цепочке Минцифры — тогда повторяем без верификации.
        self._verify = True

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Any = None) -> Any:
        async with httpx.AsyncClient(timeout=90.0, verify=self._verify) as client:
            resp = await client.request(
                method,
                MAX_API_URL + path,
                params=params,
                json=json_body,
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise RuntimeError("MAX: неверный токен (401)")
        if resp.status_code >= 400:
            raise RuntimeError(f"MAX: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError:
            return {}

    # --- базовые вызовы ---

    async def me(self) -> dict:
        return await self._request("GET", "/me")

    async def register_commands(self) -> None:
        try:
            await self._request(
                "PATCH",
                "/me/commands",
                json_body={"commands": [{"name": name, "description": desc} for name, desc in COMMANDS]},
            )
            logger.info("MAX: зарегистрировано команд: %d", len(COMMANDS))
        except Exception as e:
            logger.warning("MAX: не удалось зарегистрировать команды: %s", e)

    async def poll(self, marker: Optional[int], timeout: int = 30) -> Dict[str, Any]:
        params: Dict[str, Any] = {"timeout": timeout, "types": POLL_TYPES}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    async def send(self, chat_id: int, text: str, attachments: Optional[list] = None, fmt: Optional[str] = None) -> Optional[dict]:
        text = text or ""
        for chunk, is_last in _chunks(text, MAX_TEXT_LIMIT):
            body: Dict[str, Any] = {"text": chunk, "notify": True}
            if fmt:
                body["format"] = fmt
            if attachments and is_last:
                body["attachments"] = attachments
            data = await self._request("POST", "/messages", params={"chat_id": chat_id}, json_body=body)
        message = (data or {}).get("message") if isinstance(data, dict) else None
        return message

    async def edit(self, message_id: str, text: str, attachments: Optional[list] = None, fmt: Optional[str] = None) -> None:
        body: Dict[str, Any] = {"text": text or ""}
        if fmt:
            body["format"] = fmt
        if attachments:
            body["attachments"] = attachments
        await self._request("PUT", "/messages", params={"message_id": message_id}, json_body=body)

    async def answer_callback(self, callback_id: str, notification: Optional[str] = None) -> None:
        body: Dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        try:
            await self._request("POST", "/answers", params={"callback_id": callback_id}, json_body=body)
        except Exception as e:
            logger.warning("MAX answer_callback failed: %s", e)


def _chunks(text: str, limit: int):
    """Режет длинный текст по строкам; возвращает (кусок, это_последний)."""
    if len(text) <= limit:
        yield text, True
        return
    lines = text.split("\n")
    buf = ""
    pending: List[str] = []
    for line in lines:
        if len(line) + 1 > limit:  # одна гигантская строка
            if buf:
                pending.append(buf)
                buf = ""
            pending.append(line)
        elif len(buf) + len(line) + 1 > limit:
            pending.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        pending.append(buf)
    for i, part in enumerate(pending):
        # гигантские строки режем жёстко
        while len(part) > limit:
            yield part[:limit], False
            part = part[limit:]
        yield part, i == len(pending) - 1


# ---------------------------------------------------------------------------
# Конвертация клавиатур PTB -> MAX
# ---------------------------------------------------------------------------

def kb_to_max(kb: Any) -> List[dict]:
    """InlineKeyboardMarkup -> список attachments MAX. Прочее (ReplyKeyboardRemove) -> []."""
    rows = getattr(kb, "inline_keyboard", None)
    if not rows:
        return []
    out_rows = []
    for row in rows:
        out_row = []
        for btn in row:
            text = getattr(btn, "text", None) or "•"
            url = getattr(btn, "url", None)
            data = getattr(btn, "callback_data", None)
            if url:
                out_row.append({"type": "link", "text": text, "url": url})
            else:
                out_row.append({"type": "callback", "text": text, "payload": data or "noop"})
        if out_row:
            out_rows.append(out_row)
    if not out_rows:
        return []
    return [{"type": "inline_keyboard", "payload": {"buttons": out_rows}}]


def contact_kb() -> List[dict]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {"buttons": [[{"type": "request_contact", "text": "📱 Поделиться контактом"}]]},
        }
    ]


# ---------------------------------------------------------------------------
# Fake-объекты в стиле python-telegram-bot для переиспользования команд bot.py
# ---------------------------------------------------------------------------

class MaxMessage:
    """Эмуляция telegram.Message: reply_text / edit_text + .chat.id."""

    def __init__(self, client: MaxClient, chat_id: int, mid: Optional[str] = None, fmt: Optional[str] = None):
        self._client = client
        self.chat = SimpleNamespace(id=chat_id)
        self._mid = mid
        self._fmt = fmt

    @property
    def mid(self) -> Optional[str]:
        return self._mid

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None, **_: Any) -> "MaxMessage":
        msg = await self._client.send(int(self.chat.id), text, kb_to_max(reply_markup), _fmt(parse_mode))
        mid = None
        if isinstance(msg, dict):
            mid = ((msg.get("body") or {}).get("mid")) or None
        return MaxMessage(self._client, self.chat.id, mid, _fmt(parse_mode))

    async def edit_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None, **_: Any) -> None:
        if not self._mid:
            raise RuntimeError("no message id to edit")
        await self._client.edit(self._mid, text, kb_to_max(reply_markup), _fmt(parse_mode))


def _fmt(parse_mode: Any) -> Optional[str]:
    # ParseMode.HTML у PTB совместим с ограниченным HTML MAX (b/i/u/s/a/code).
    if parse_mode is None:
        return None
    name = getattr(parse_mode, "value", None) or str(parse_mode)
    return "html" if name.lower().startswith("html") else None


class FakeBot:
    """Эмуляция application.bot для send_to_admins/periodic-циклов."""

    def __init__(self, client: MaxClient):
        self._client = client

    async def send_message(self, chat_id: int, text: str, parse_mode: Any = None, reply_markup: Any = None, **_: Any) -> None:
        await self._client.send(int(chat_id), text, kb_to_max(reply_markup), _fmt(parse_mode))


class FakeApp:
    def __init__(self, client: MaxClient):
        self.bot = FakeBot(client)


def fake_update(client: MaxClient, chat_id: int, text: str = "", mid: Optional[str] = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        message=MaxMessage(client, chat_id, mid),
    )


def fake_context(app: FakeApp, args: Optional[List[str]] = None) -> SimpleNamespace:
    return SimpleNamespace(args=args or [], application=app)


class FakeQuery:
    """Эмуляция telegram.CallbackQuery для bot.callback_handler."""

    def __init__(self, client: MaxClient, app: FakeApp, chat_id: int, payload: str, message: MaxMessage):
        self.data = payload
        self.message = message
        self.from_user = SimpleNamespace(id=int(chat_id))
        self._client = client
        self._app = app

    async def answer(self, *args: Any, **kwargs: Any) -> None:  # query.answer() в MAX не нужен
        return


# ---------------------------------------------------------------------------
# Приватность: белый список по номеру телефона / user_id
# ---------------------------------------------------------------------------

def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def allowed_phones() -> List[str]:
    return [normalize_phone(p) for p in MAX_ADMIN_PHONES if normalize_phone(p)]


def extract_contact_phone(attachments: Any) -> str:
    for att in attachments or []:
        if not isinstance(att, dict) or att.get("type") != "contact":
            continue
        payload = att.get("payload") or {}
        vcf = payload.get("vcf_info") or ""
        if vcf:
            for line in vcf.splitlines():
                if line.upper().startswith("TEL"):
                    phone = line.split(":", 1)[1] if ":" in line else ""
                    if phone.strip():
                        return phone.strip()
        info = payload.get("max_info") or {}
        phone = (info.get("phone") or "").strip() if isinstance(info, dict) else ""
        if phone:
            return phone
    return ""


async def is_authorized(chat_id: int, user_id: Optional[int]) -> bool:
    state = await bot.load_state()
    if int(chat_id) in (state.get("admins") or []):
        return True
    if user_id and int(user_id) in MAX_ADMIN_USER_IDS:
        state.setdefault("admins", []).append(int(chat_id))
        await bot.storage.save(state)
        return True
    return False


async def authorize_by_contact(client: MaxClient, chat_id: int, phone: str) -> bool:
    if normalize_phone(phone) not in allowed_phones():
        return False
    state = await bot.load_state()
    admins = state.setdefault("admins", [])
    if int(chat_id) not in admins:
        admins.append(int(chat_id))
        await bot.storage.save(state)
    return True


# ---------------------------------------------------------------------------
# Обработка событий MAX
# ---------------------------------------------------------------------------

COMMAND_RE = re.compile(r"^/([a-zA-Z_]+)(?:@\w+)?(?:\s+(.*))?$", re.S)

# Таблица команд bot.py: имя -> хендлер (как в bot.main()).
HANDLERS: Dict[str, Any] = {
    "start": bot.cmd_start,
    "help": bot.cmd_help,
    "sync": bot.cmd_sync,
    "check": bot.cmd_check,
    "list": bot.cmd_list,
    "stock": bot.cmd_stock,
    "stocks": bot.cmd_stock,
    "returns": bot.cmd_returns,
    "prices": bot.cmd_prices,
    "price": bot.cmd_prices,
    "index": bot.cmd_index,
    "indexes": bot.cmd_index,
    "setinterval": bot.cmd_setinterval,
    "setreporttime": bot.cmd_setreporttime,
    "setordersdays": bot.cmd_setordersdays,
    "report": bot.cmd_report,
    "orders": bot.cmd_orders,
    "order": bot.cmd_orders,
    "deliveries": bot.cmd_deliveries,
    "delivery": bot.cmd_deliveries,
    "shipping": bot.cmd_deliveries,
    "ym": bot.cmd_ym,
    "yandexmarket": bot.cmd_ym,
    "tilda": bot.cmd_tilda,
    "tildaset": bot.cmd_tildaset,
    "returnstats": bot.cmd_returnstats,
    "reviews": bot.cmd_reviews,
    "review": bot.cmd_reviews,
    "add": bot.cmd_add,
    "remove": bot.cmd_remove,
    "setthreshold": bot.cmd_setthreshold,
    "setstockthreshold": bot.cmd_setstockthreshold,
    "status": bot.cmd_status,
    "admins": bot.cmd_admins,
    "settings": bot.cmd_settings,
}

TEXT_BUTTONS = {
    "🔄 Синхронизировать": "sync",
    "✅ Проверить": "check",
    "📋 Товары": "list",
    "📦 Остатки": "stock",
    "↩️ Возвраты": "returns",
    "⚙️ Настройки": "settings",
    "❓ Помощь": "help",
}


async def handle_message_created(client: MaxClient, app: FakeApp, update: dict) -> None:
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id")
    sender = message.get("sender") or {}
    user_id = sender.get("user_id")
    if chat_id is None:
        return
    chat_id = int(chat_id)
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    mid = (body.get("mid")) or None

    if not await is_authorized(chat_id, user_id):
        phone = extract_contact_phone(body.get("attachments"))
        if phone and await authorize_by_contact(client, chat_id, phone):
            await client.send(chat_id, "✅ Номер подтверждён. Добро пожаловать!")
            await bot.cmd_start(fake_update(client, chat_id, text, mid), fake_context(app))
            return
        if phone:
            await client.send(
                chat_id,
                "Извините, этот бот — служебный: он показывает заказы, остатки и доставки только владельцу магазина.",
                contact_kb(),
            )
            return
        await client.send(
            chat_id,
            "Бот работает только для владельца магазина.\n"
            "Нажмите кнопку и поделитесь контактом — если это вы, доступ откроется.",
            contact_kb(),
        )
        return

    # Команды
    match = COMMAND_RE.match(text)
    if match:
        name = match.group(1).lower()
        raw_args = (match.group(2) or "").split()
        args = [a for a in raw_args if a]
        handler = HANDLERS.get(name)
        if handler is None:
            await client.send(chat_id, "Неизвестная команда. Смотрите /help")
            return
        try:
            await handler(fake_update(client, chat_id, text, mid), fake_context(app, args))
        except Exception:
            logger.exception("MAX command /%s failed", name)
            await client.send(chat_id, "⚠️ Ошибка выполнения команды, подробности в логе.")
        return

    # Кнопки нижнего меню Telegram («📦 Остатки» и т.п.) в MAX приходят текстом
    target = TEXT_BUTTONS.get(text)
    if target:
        handler = HANDLERS.get(target)
        if handler is not None:
            try:
                await handler(fake_update(client, chat_id, text, mid), fake_context(app))
            except Exception:
                logger.exception("MAX text button %s failed", target)
            return

    if text:
        await bot.show_main_menu(MaxMessage(client, chat_id, mid))


async def handle_callback(client: MaxClient, app: FakeApp, update: dict) -> None:
    callback = update.get("callback") or {}
    payload = callback.get("payload") or ""
    callback_id = callback.get("callback_id") or ""
    user_id = ((callback.get("user") or {}).get("user_id"))
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id")
    if chat_id is None:
        return
    chat_id = int(chat_id)

    if not await is_authorized(chat_id, user_id):
        await client.answer_callback(callback_id, "Доступ запрещён")
        return

    body = message.get("body") or {}
    mid = body.get("mid")
    fake_msg = MaxMessage(client, chat_id, mid)
    query = FakeQuery(client, app, chat_id, payload, fake_msg)
    try:
        await bot.callback_handler(SimpleNamespace(callback_query=query), SimpleNamespace(application=app))
    except Exception:
        logger.exception("MAX callback %s failed", payload)
        await client.answer_callback(callback_id, "Ошибка")


async def handle_bot_started(client: MaxClient, app: FakeApp, update: dict) -> None:
    chat_id = update.get("chat_id")
    user = update.get("user") or {}
    if chat_id is None:
        return
    if await is_authorized(int(chat_id), user.get("user_id")):
        await bot.cmd_start(fake_update(client, int(chat_id)), fake_context(app))
    else:
        await client.send(int(chat_id), "Бот работает только для владельца магазина. Поделитесь контактом:", contact_kb())


# ---------------------------------------------------------------------------
# Приложение: long polling + periodic-циклы общего движка
# ---------------------------------------------------------------------------

async def updates_loop(client: MaxClient, app: FakeApp) -> None:
    marker: Optional[int] = None
    while True:
        try:
            data = await client.poll(marker, timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("MAX poll failed: %s", e)
            await asyncio.sleep(5)
            continue
        marker = data.get("marker")
        for update in data.get("updates") or []:
            utype = update.get("update_type")
            try:
                if utype == "message_created":
                    await handle_message_created(client, app, update)
                elif utype == "message_callback":
                    await handle_callback(client, app, update)
                elif utype == "bot_started":
                    await handle_bot_started(client, app, update)
            except Exception:
                logger.exception("MAX update handling failed: %s", utype)


async def main_async() -> None:
    if not MAX_BOT_TOKEN:
        logger.error("MAX_BOT_TOKEN is not set in .env")
        return
    client = MaxClient(MAX_BOT_TOKEN)
    try:
        me = await client.me()
    except Exception as e:
        logger.error("MAX недоступен: %s", e)
        return
    logger.info("MAX bot started: @%s (user_id=%s)", me.get("username"), me.get("user_id"))
    await client.register_commands()

    app = FakeApp(client)
    # Те же periodic-циклы, что у Telegram-бота (цены, доставки, Тильда, отчёт).
    asyncio.create_task(bot.periodic_loop(app))
    asyncio.create_task(bot.startup_check(app))
    await updates_loop(client, app)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
