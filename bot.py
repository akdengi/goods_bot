#!/usr/bin/env python3
"""Ozon goods price watcher Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "").strip()
OZON_API_KEY = os.getenv("OZON_API_KEY", "").strip()
YANDEX_MARKET_TOKEN = os.getenv("YANDEX_MARKET_API_KEY", "").strip() or os.getenv("YANDEX_MARKET_TOKEN", "").strip()
YANDEX_MARKET_CAMPAIGN_ID_RAW = os.getenv("YANDEX_MARKET_CAMPAIGN_ID", "").strip()
YANDEX_DELIVERY_TOKEN = os.getenv("YANDEX_DELIVERY_TOKEN", "").strip()
CDEK_ACCOUNT = (os.getenv("CDEK_ACCOUNT", "") or os.getenv("CDEK_CLIENT_ID", "")).strip()
CDEK_SECURE_PASSWORD = (os.getenv("CDEK_SECURE_PASSWORD", "") or os.getenv("CDEK_CLIENT_SECRET", "")).strip()
CDEK_V1_URL = os.getenv("CDEK_V1_URL", "https://integration.cdek.ru").strip() or "https://integration.cdek.ru"
CDEK_V2_URL = os.getenv("CDEK_V2_URL", "https://api.cdek.ru").strip() or "https://api.cdek.ru"
CDEK_STORAGE_DAYS = int(os.getenv("CDEK_STORAGE_DAYS", "7") or 7)
DELIVERY_STORAGE_WARN_HOURS = int(os.getenv("DELIVERY_STORAGE_WARN_HOURS", "24") or 24)
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
THRESHOLD_RUB = float(os.getenv("THRESHOLD_RUB", "50") or 50)
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "900") or 900)
STOCK_ALERT_THRESHOLD = int(os.getenv("STOCK_ALERT_THRESHOLD", "0") or 0)
DATA_DIR = os.getenv("DATA_DIR", "").strip() or str(BASE_DIR / "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

sys.path.insert(0, str(BASE_DIR))
from ozon import OzonClient, OzonError  # noqa: E402
from yandex_market import YandexMarketClient, YandexMarketError  # noqa: E402
from shipping import ShippingError, build_shipping_service, parse_dt  # noqa: E402
from storage import Storage  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ozon_goods_bot")

storage = Storage(STATE_FILE)
ozon = OzonClient(OZON_CLIENT_ID, OZON_API_KEY)
try:
    yandex_market_campaign_id = int(YANDEX_MARKET_CAMPAIGN_ID_RAW) if YANDEX_MARKET_CAMPAIGN_ID_RAW else None
except (TypeError, ValueError):
    yandex_market_campaign_id = None
ym_client = YandexMarketClient(YANDEX_MARKET_TOKEN, yandex_market_campaign_id)
shipping = build_shipping_service(
    ozon_client=ozon,
    yandex_token=YANDEX_DELIVERY_TOKEN,
    cdek_account=CDEK_ACCOUNT,
    cdek_secure=CDEK_SECURE_PASSWORD,
    cdek_v1_url=CDEK_V1_URL,
    cdek_v2_url=CDEK_V2_URL,
    cdek_storage_days=CDEK_STORAGE_DAYS,
    yandex_market_token=YANDEX_MARKET_TOKEN,
    yandex_market_campaign_id=yandex_market_campaign_id,
)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔄 Синхронизировать"), KeyboardButton("✅ Проверить")],
        [KeyboardButton("📋 Товары"), KeyboardButton("📦 Остатки"), KeyboardButton("↩️ Возвраты")],
        [KeyboardButton("⚙️ Настройки"), KeyboardButton("❓ Помощь")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

PAGE_SIZE = 8

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(v)


def fmt_date(v: Any) -> str:
    if not v:
        return "—"
    try:
        s = str(v).replace("Z", "+00:00")
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(v)


def fmt_delivery_date(v: Any) -> str:
    """For dates that may be date-only (CDEK planned_delivery_date)."""
    if not v:
        return "—"
    s = str(v).strip()
    if len(s) <= 10:
        dt = parse_dt(s)
        return dt.astimezone().strftime("%d.%m.%Y") if dt else s
    return fmt_date(s)


def short_text(v: Any, limit: int = 70) -> str:
    s = str(v or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"

ORDER_STATUS_LABELS = {
    "awaiting_packaging": "Ожидает сборки",
    "delivering": "Доставляется",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
    "posting_created": "Ожидает сборки",
    "posting_on_way_to_city": "Доставляется",
    "posting_received": "Доставлен",
    "posting_canceled": "Отменён",
}

async def load_state() -> Dict[str, Any]:
    state = await storage.load()
    state.setdefault("check_interval", CHECK_INTERVAL_SEC)
    state.setdefault("seen_review_ids", [])
    state.setdefault("reviews_initialized", False)
    state.setdefault("last_daily_report_date", "")
    state.setdefault("report_hour_local", int(os.getenv("DAILY_REPORT_HOUR", "10") or 10))
    state.setdefault("orders_days", int(os.getenv("ORDERS_DAYS", "3") or 3))
    state.setdefault("stock_monitoring_enabled", True)
    state.setdefault("store_mon_ozon", bool(OZON_CLIENT_ID and OZON_API_KEY))
    state.setdefault("store_mon_ym", bool(YANDEX_MARKET_TOKEN))
    state.setdefault("ym_offers", {})
    state.setdefault("ym_orders_seen", [])
    state.setdefault("ym_offers_initialized", False)
    state.setdefault("ym_orders_initialized", False)
    state.setdefault("store_mon_ym", bool(YANDEX_MARKET_TOKEN))
    state.setdefault("ym_offers", {})
    state.setdefault("ym_orders_seen", [])
    state.setdefault("ym_offers_initialized", False)
    state.setdefault("ym_orders_initialized", False)
    state.setdefault("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS)
    state.setdefault("delivery_alerts", {})
    if ADMIN_CHAT_ID_RAW:
        try:
            cid = int(ADMIN_CHAT_ID_RAW)
            if cid not in state["admins"]:
                state["admins"].append(cid)
                await storage.save(state)
        except ValueError:
            logger.warning("ADMIN_CHAT_ID is not an integer: %s", ADMIN_CHAT_ID_RAW)
    return state

def is_admin(chat_id: int, state: Dict[str, Any]) -> bool:
    return chat_id in (state.get("admins") or [])

async def send_to_admins(context_or_app, text: str) -> None:
    state = await load_state()
    for cid in state.get("admins") or []:
        try:
            kb = inline_main_kb()
            if hasattr(context_or_app, "bot"):
                await context_or_app.bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await context_or_app.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", cid, e)

async def _admin_guard(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    state = await load_state()
    if not state.get("admins"):
        state["admins"].append(chat.id)
        await storage.save(state)
        return True
    if chat.id not in state.get("admins", []):
        await update.message.reply_text("Доступ запрещён.")
        return False
    return True

async def run_price_check(application: Application) -> None:
    """Periodic: prices + stocks + new returns. Notifies only on changes."""
    state = await load_state()
    threshold = float(state.get("threshold") or THRESHOLD_RUB)
    stock_threshold = int(state.get("stock_threshold", STOCK_ALERT_THRESHOLD) or 0)
    tracked = state.get("tracked") or {}
    ozon_store_enabled = bool(state.get("store_mon_ozon", True)) and ozon.configured()
    tracked_ids = []
    if ozon_store_enabled:
        for k in tracked:
            try:
                tracked_ids.append(int(k))
            except (ValueError, TypeError):
                pass

    price_map: Dict[int, Dict[str, Any]] = {}
    price_items: List[Dict[str, Any]] = []
    if ozon_store_enabled:
        # --- prices ---
        try:
            price_items = await ozon.fetch_prices()
            for it in price_items:
                pid = it.get("product_id")
                if pid is None:
                    continue
                try:
                    price_map[int(pid)] = it
                except (ValueError, TypeError):
                    continue
        except OzonError as e:
            logger.error("Periodic price Ozon error: %s", e)
            await send_to_admins(application, f"⚠️ Ошибка Ozon API (цены): {e}")
            price_items = []

    # --- stocks ---
    stock_map: Dict[int, Dict[str, Any]] = {}
    if tracked_ids:
        try:
            stock_map = await ozon.fetch_stocks(tracked_ids)
        except OzonError as e:
            logger.error("Periodic stock Ozon error: %s", e)
            await send_to_admins(application, f"⚠️ Ошибка Ozon API (остатки): {e}")

    # --- returns ---
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            ym_orders = await ym_client.fetch_orders(limit=100)
            ym_recent: List[Dict[str, Any]] = []
            for order in ym_orders:
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                dt = None
                if isinstance(created, str):
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = None
                if dt is None or dt >= since_dt:
                    ym_recent.append(order)
            counts: Dict[str, int] = {}
            for offer in ym_offers:
                st = str(offer.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            low = [o for o in ym_offers if str(o.get("status") or "").upper() in ("NO_STOCKS", "DISABLED_BY_PARTNER") or (isinstance(o.get("stock"), int) and int(o.get("stock") or 0) <= 0)]
            lines.append("")
            lines.append(f"🟡 Яндекс Маркет: офферов {len(ym_offers)}, заказов за 24ч {len(ym_recent)}")
            if counts:
                lines.append("Статусы YM: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if low:
                lines.append(f"Офферы без остатка/выключенные YM: {len(low)}")
                for o in low[:8]:
                    lines.append(f"• {str(o.get('name') or o.get('offer_id'))[:60]} — статус {o.get('status')}, остаток {o.get('stock') if o.get('stock') is not None else '—'}")
        except Exception as e:
            logger.warning("YM daily report failed: %s", e)
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            ym_orders = await ym_client.fetch_orders(limit=100)
            ym_recent: List[Dict[str, Any]] = []
            for order in ym_orders:
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                dt = None
                if isinstance(created, str):
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = None
                if dt is None or dt >= since_dt:
                    ym_recent.append(order)
            counts: Dict[str, int] = {}
            for offer in ym_offers:
                st = str(offer.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            low = [o for o in ym_offers if str(o.get("status") or "").upper() in ("NO_STOCKS", "DISABLED_BY_PARTNER") or (isinstance(o.get("stock"), int) and int(o.get("stock") or 0) <= 0)]
            lines.append("")
            lines.append(f"🟡 Яндекс Маркет: офферы {len(ym_offers)}, заказов за 24ч {len(ym_recent)}")
            if counts:
                lines.append("Статусы YM: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if low:
                lines.append(f"Офферы без остатка/выключенные YM: {len(low)}")
                for o in low[:8]:
                    lines.append(f"• {str(o.get('name') or o.get('offer_id'))[:60]} — статус {o.get('status')}, остаток {o.get('stock') if o.get('stock') is not None else '—'}")
        except Exception as e:
            logger.warning("YM daily report failed: %s", e)
            lines.append(f"🟡 Яндекс Маркет недоступен: {e}")

    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            ym_orders = await ym_client.fetch_orders(limit=100)
            ym_recent: List[Dict[str, Any]] = []
            for order in ym_orders:
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                dt = None
                if isinstance(created, str):
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = None
                if dt is None or dt >= since_dt:
                    ym_recent.append(order)
            counts: Dict[str, int] = {}
            for offer in ym_offers:
                st = str(offer.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            low = [o for o in ym_offers if str(o.get("status") or "").upper() in ("NO_STOCKS", "DISABLED_BY_PARTNER") or (isinstance(o.get("stock"), int) and int(o.get("stock") or 0) <= 0)]
            lines.append("")
            lines.append(f"🟡 Яндекс Маркет: офферов {len(ym_offers)}, заказов за 24ч {len(ym_recent)}")
            if counts:
                lines.append("Статусы YM: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if low:
                lines.append(f"Офферы без остатка/выключенные YM: {len(low)}")
                for o in low[:8]:
                    lines.append(f"• {str(o.get('name') or o.get('offer_id'))[:60]} — статус {o.get('status')}, остаток {o.get('stock') if o.get('stock') is not None else '—'}")
        except Exception as e:
            logger.warning("YM daily report failed: %s", e)
            lines.append(f"🟡 Яндекс Маркет недоступен: {e}")


    returns_list: List[Dict[str, Any]] = []
    returns_fetch_ok = True
    if ozon_store_enabled:
        try:
            returns_list = await ozon.fetch_returns(limit=100)
        except OzonError as e:
            returns_fetch_ok = False
            logger.error("Periodic returns Ozon error: %s", e)
            await send_to_admins(application, f"⚠️ Ошибка Ozon API (возвраты): {e}")

    price_changes: List[Dict[str, Any]] = []
    stock_alerts: List[Dict[str, Any]] = []
    now = now_iso()

    for pid_key, meta in list(tracked.items()):
        try:
            pid = int(pid_key)
        except (ValueError, TypeError):
            continue
        # price handling
        it = price_map.get(pid)
        if it is not None:
            current_price = it.get("price")
            try:
                current_price = float(current_price) if current_price is not None else None
            except (TypeError, ValueError):
                current_price = None
            if current_price is not None:
                last_price = meta.get("last_price")
                meta["offer_id"] = it.get("offer_id") or meta.get("offer_id")
                meta["last_checked"] = now
                meta["last_status"] = "ok"
                if last_price is None:
                    meta["last_price"] = current_price
                else:
                    try:
                        last_price = float(last_price)
                    except (TypeError, ValueError):
                        last_price = None
                    if last_price is not None:
                        diff = current_price - last_price
                        if abs(diff) > threshold:
                            price_changes.append(
                                {
                                    "product_id": pid,
                                    "name": meta.get("name") or f"Товар {pid}",
                                    "offer_id": meta.get("offer_id"),
                                    "old": last_price,
                                    "new": current_price,
                                    "diff": diff,
                                }
                            )
                    meta["last_price"] = current_price

        # stock handling
        st = stock_map.get(pid)
        if tracked_ids and st is not None:
            for typ in ("fbo", "fbs"):
                cur = st.get(typ)
                prev = meta.get(typ)
                if prev is None and meta.get("stock_initialized") is not True:
                    # baseline only
                    pass
                if prev is not None and cur is not None and state.get("stock_monitoring_enabled", True):
                    if prev > 0 and (cur <= stock_threshold):
                        stock_alerts.append(
                            {
                                "product_id": pid,
                                "name": meta.get("name") or f"Товар {pid}",
                                "offer_id": meta.get("offer_id"),
                                "type": typ.upper(),
                                "prev": prev,
                                "cur": cur,
                            }
                        )
                meta[typ] = cur
        elif st is not None:
            for typ in ("fbo", "fbs"):
                meta[typ] = st.get(typ)

    # mark stock_initialized after first run for all tracked
    for meta in tracked.values():
        meta["stock_initialized"] = True

    state["tracked"] = tracked
    state["last_check"] = now

    # price alerts
    if price_changes:
        lines = [f"🚨 <b>Изменение цен на Ozon</b> (порог {fmt_money(threshold)} ₽)\n"]
        for c in price_changes:
            ident = c["name"]
            if c.get("offer_id"):
                ident += f" — {c['offer_id']}"
            sign = "+" if c["diff"] > 0 else ""
            lines.append(
                f"• {ident}\n"
                f"  Было: {fmt_money(c['old'])} ₽ → Стало: {fmt_money(c['new'])} ₽ ({sign}{fmt_money(c['diff'])} ₽)"
            )
        await send_to_admins(application, "\n".join(lines))

    # stock alerts
    if stock_alerts:
        lines = ["📦 <b>Закончились остатки на Ozon</b>\n"]
        for a in stock_alerts:
            ident = a["name"]
            if a.get("offer_id"):
                ident += f" — {a['offer_id']}"
            lines.append(f"• {ident}: {a['type']} было {a['prev']} → стало {a['cur']}")
        await send_to_admins(application, "\n".join(lines))

    # returns alerts
    seen_returns = set(state.get("seen_return_ids") or [])
    returns_initialized = bool(state.get("returns_initialized"))
    return_alerts: List[Dict[str, Any]] = []
    new_ids = set()
    for r in returns_list:
        rid = r.get("id")
        if rid is None:
            continue
        new_ids.add(rid)
        if returns_initialized and rid not in seen_returns:
            return_alerts.append(r)
    # baseline on first successful returns fetch
    if returns_list and not returns_initialized:
        seen_returns = new_ids
        state["returns_initialized"] = True
    else:
        seen_returns |= new_ids
    if returns_fetch_ok and not returns_initialized:
        state["returns_initialized"] = True
    # --- reviews ---
    review_alerts: List[Dict[str, Any]] = []
    reviews_list: List[Dict[str, Any]] = []
    try:
        reviews_list = await ozon.fetch_reviews(limit=100)
        reviews_list.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    except OzonError as e:
        logger.warning("reviews fetch failed: %s", e)
    sku_info: Dict[int, Dict[str, Any]] = {}
    if reviews_list:
        skus = [r.get("sku") for r in reviews_list if isinstance(r, dict) and r.get("sku") is not None]
        try:
            sku_info = await ozon.get_names_by_sku([int(x) for x in skus if x is not None])
        except Exception:
            sku_info = {}
    seen_review_ids = set(state.get("seen_review_ids") or [])
    new_seen_review_ids = set(seen_review_ids)
    reviews_initialized_local = bool(state.get("reviews_initialized"))
    for r in reviews_list:
        rid = r.get("id")
        if rid is None:
            continue
        if reviews_initialized_local and rid not in seen_review_ids:
            review_alerts.append(r)
        new_seen_review_ids.add(rid)
    state["seen_review_ids"] = list(new_seen_review_ids)[-5000:]
    if reviews_list or not reviews_initialized_local:
        state["reviews_initialized"] = True

    state["seen_return_ids"] = list(seen_returns)[-5000:]
    from datetime import timedelta as _td
    order_alerts = []
    orders_list = []
    if ozon_store_enabled:
        try:
            _since = (datetime.now(timezone.utc) - _td(days=7)).isoformat()
            _to = now_iso()
            orders_list = await ozon.fetch_postings_fbs(_since, _to, 50) + await ozon.fetch_postings_fbo(_since, _to, 50)
        except Exception as e:
            logger.warning("orders fetch failed: %s", e)
    known_postings = state.get("postings") or {}
    seen_numbers = set(known_postings.keys())
    orders_initialized = bool(state.get("orders_initialized"))
    new_known = dict(known_postings)
    for p in orders_list:
        pn = p.get("posting_number")
        if pn is None:
            continue
        status = p.get("status") or p.get("substatus") or ""
        products = p.get("products", []) or []
        product_names = ", ".join(str(x.get("name") or x.get("offer_id") or "") for x in products if isinstance(x, dict))
        price = None
        if products and isinstance(products[0], dict):
            pr = products[0].get("price", {})
            if isinstance(pr, dict):
                price = pr.get("amount")
        detail = {"status": status, "created_at": p.get("created_at") or p.get("in_process_at"), "products": product_names, "price": price}
        if orders_initialized:
            if pn not in seen_numbers:
                order_alerts.append(p)
            else:
                prev_status = (known_postings.get(pn) or {}).get("status")
                if prev_status and status and str(status).lower() != str(prev_status).lower() and "cancel" in str(status).lower():
                    order_alerts.append(p)
        new_known[pn] = detail
    if len(new_known) > 300:
        sorted_items = sorted(new_known.items(), key=lambda kv: kv[1].get("created_at") or "", reverse=True)
        new_known = dict(sorted_items[:300])
    state["postings"] = new_known
    if orders_list or not orders_initialized:
        state["orders_initialized"] = True
    # --- Yandex Market offers/orders ---
    ym_alert_lines: List[str] = []
    if state.get("store_mon_ym", True) and ym_client.configured():
        ym_offers_list: List[Dict[str, Any]] = []
        ym_orders_list: List[Dict[str, Any]] = []
        try:
            ym_offers_list = await ym_client.fetch_offers()
        except Exception as e:
            logger.warning("YM offers fetch failed: %s", e)
        try:
            ym_orders_list = await ym_client.fetch_orders()
        except Exception as e:
            logger.warning("YM orders fetch failed: %s", e)

        ym_known = state.get("ym_offers") or {}
        ym_orders_seen = set(state.get("ym_orders_seen") or [])
        ym_offers_initialized = bool(state.get("ym_offers_initialized", False))
        ym_orders_initialized = bool(state.get("ym_orders_initialized", False))
        new_ym_known = dict(ym_known)
        new_ym_orders_seen = set(ym_orders_seen)
        ym_offer_alerts: List[Dict[str, Any]] = []
        for offer in ym_offers_list:
            oid = offer.get("offer_id")
            if oid is None:
                continue
            price = offer.get("basic_price")
            status = offer.get("status")
            detail = {"name": offer.get("name"), "price": price, "status": status}
            prev = ym_known.get(oid)
            if ym_offers_initialized:
                if oid not in ym_known:
                    ym_offer_alerts.append({"type": "новый оффер", "name": offer.get("name"), "offer_id": oid, "price": price, "status": status})
                elif prev:
                    try:
                        prev_price = float(prev.get("price")) if prev.get("price") is not None else None
                    except (TypeError, ValueError):
                        prev_price = None
                    try:
                        cur_price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        cur_price = None
                    if (cur_price is not None and prev_price is not None and abs(cur_price - prev_price) > threshold) or str(status).lower() != str(prev.get("status")).lower():
                        ym_offer_alerts.append({"type": "изменение оффера", "name": offer.get("name"), "offer_id": oid, "old_price": prev_price, "price": cur_price, "old_status": prev.get("status"), "status": status})
            new_ym_known[oid] = detail

        ym_order_alerts: List[Dict[str, Any]] = []
        for order in ym_orders_list:
            oid = order.get("id") or order.get("orderId") or order.get("order_id")
            if oid is None:
                continue
            oid = str(oid)
            if ym_orders_initialized and oid not in ym_orders_seen:
                ym_order_alerts.append(order)
            new_ym_orders_seen.add(oid)

        state["ym_offers"] = new_ym_known
        state["ym_orders_seen"] = list(new_ym_orders_seen)[-2000:]
        if ym_offers_list or not ym_offers_initialized:
            state["ym_offers_initialized"] = True
        if ym_orders_list or not ym_orders_initialized:
            state["ym_orders_initialized"] = True

        if ym_offer_alerts and ym_offers_initialized:
            lines = [f"🟡 <b>Yandex Market: изменения офферов</b> ({len(ym_offer_alerts)}):"]
            for o in ym_offer_alerts[:20]:
                lines.append(
                    f"• {html_escape(str(o.get('name') or o.get('offer_id')))[:80]} ({o.get('offer_id')})\n"
                    f"  Тип: {o.get('type')}; статус: {o.get('status')}\n"
                    + (f"  Цена: {fmt_money(o.get('price'))} ₽\n" if o.get("price") is not None else "")
                )
            await send_to_admins(application, "\n".join(lines))
        if ym_order_alerts:
            lines = [f"🛒 <b>Yandex Market: новые заказы</b> ({len(ym_order_alerts)}):"]
            for o in ym_order_alerts[:20]:
                lines.append(f"• {o.get('id') or o.get('orderId') or o.get('order_id')} — {o.get('status')}")
            await send_to_admins(application, "\n".join(lines))

    # order_alerts_phase_daily
    state["last_check_summary"] = {
        "checked": len(tracked),
        "price_changes": len(price_changes),
        "stock_alerts": len(stock_alerts),
        "new_returns": len(return_alerts),
        "new_orders": len(order_alerts),
        "new_reviews": len(review_alerts),
    }
    await storage.save(state)

    if return_alerts:
        lines = [f"↩️ <b>Новые возвраты на Ozon</b>: {len(return_alerts)}\n"]
        for r in return_alerts[:20]:
            prod = r.get("product", {}) if isinstance(r.get("product"), dict) else {}
            name = prod.get("name") or prod.get("offer_id") or "товар"
            offer = prod.get("offer_id")
            schema = r.get("schema")
            rtype = r.get("type")
            reason = r.get("return_reason_name")
            qty = prod.get("quantity")
            order = r.get("order_number")
            status = ""
            vis = r.get("visual", {})
            if isinstance(vis, dict) and isinstance(vis.get("status"), dict):
                status = vis["status"].get("display_name") or vis["status"].get("sys_name") or ""
            lines.append(
                f"• {name}" + (f" ({offer})" if offer else "") + "\n"
                f"  Схема/тип: {schema} / {rtype}; кол-во: {qty}; статус: {status}\n"
                f"  Причина: {reason}\n"
                f"  Заказ: {order}; Возврат #{r.get('id')}\n"
            )
        if len(return_alerts) > 20:
            lines.append(f"... и ещё {len(return_alerts) - 20}")
        await send_to_admins(application, "\n".join(lines))

    if review_alerts:
        lines = [f"⭐ <b>Новые отзывы</b>: {len(review_alerts)}\n"]
        for r in review_alerts[:10]:
            sku = r.get("sku")
            sku_i = int(sku) if isinstance(sku, (int, str)) and str(sku).isdigit() else None
            info = sku_info.get(sku_i) if sku_i is not None else {}
            name = (info or {}).get("name") or f"SKU {sku}"
            rating = r.get("rating")
            text = str(r.get("text") or "").replace("\n", " ")[:180]
            published = fmt_date(r.get("published_at"))
            lines.append(f"• {name}: ⭐{rating}\n  {text}\n  опубликовано: {published}")
        if len(review_alerts) > 10:
            lines.append(f"... и ещё {len(review_alerts) - 10}")
        await send_to_admins(application, "\n".join(lines))

    if order_alerts:
        lines = [f"🛒 <b>Новые/обновлённые заказы</b> ({len(order_alerts)}):"]
        for p in order_alerts[:20]:
            pn = p.get("posting_number")
            scheme = (p.get("delivery_scheme") or "").upper() or ("FBO" if p.get("storage_type") is not None or not p.get("delivery_method") else "FBS")
            status_raw = p.get("status") or p.get("substatus") or ""
            status = ORDER_STATUS_LABELS.get(str(status_raw).lower(), str(status_raw))
            created = fmt_date(p.get("created_at") or p.get("in_process_at"))
            products = p.get("products", []) or []
            names = ", ".join(str(x.get("name") or x.get("offer_id")) for x in products if isinstance(x, dict))[:120]
            price = None
            if products and isinstance(products[0], dict):
                pr = products[0].get("price", {})
                if isinstance(pr, dict):
                    price = pr.get("amount")
            lines.append(
                f"• {scheme} {pn}\n"
                f"  Статус: {status}\n"
                f"  Товар: {names}\n"
                f"  Создан: {created}" + (f"\n  Цена: {fmt_money(price)} ₽" if price else "")
            )
        if len(order_alerts) > 20:
            lines.append(f"… и ещё {len(order_alerts) - 20}")
        await send_to_admins(application, "\n".join(lines))
    if stock_alerts:
        logger.info("Stock alerts: %d", len(stock_alerts))
    if returns_list:
        logger.info("Returns fetched: %d, new: %d", len(returns_list), len(return_alerts))

async def send_daily_report(application: Application) -> None:
    state = await load_state()
    from datetime import timedelta
    since_dt = datetime.now(timezone.utc) - timedelta(days=1)
    since = since_dt.isoformat()
    to = now_iso()
    posts: List[Dict[str, Any]] = []
    try:
        posts = await ozon.fetch_postings_fbs(since, to, 100) + await ozon.fetch_postings_fbo(since, to, 100)
    except Exception as e:
        logger.warning("daily report postings failed: %s", e)
    count = len(posts)
    revenue = 0.0
    prod_agg: Dict[str, Dict[str, Any]] = {}
    for p in posts:
        for prod in (p.get("products", []) or []):
            if not isinstance(prod, dict):
                continue
            name = prod.get("name") or prod.get("offer_id") or f"SKU {prod.get('sku')}"
            try:
                qty = int(prod.get("quantity") or 1)
            except (TypeError, ValueError):
                qty = 1
            amt = 0.0
            pr = prod.get("price", {})
            if isinstance(pr, dict):
                try:
                    amt = float(pr.get("amount", 0) or 0)
                except (TypeError, ValueError):
                    amt = 0.0
            revenue += amt * qty
            agg = prod_agg.setdefault(str(name), {"qty": 0, "rev": 0.0})
            agg["qty"] += qty
            agg["rev"] += amt * qty
    top = sorted(prod_agg.items(), key=lambda kv: kv[1]["rev"], reverse=True)[:5]

    tracked = state.get("tracked") or {}
    stock_ids = []
    for meta in tracked.values():
        try:
            if meta.get("product_id") is not None:
                stock_ids.append(int(meta.get("product_id")))
        except (TypeError, ValueError):
            pass
    stock_map = {}
    if stock_ids:
        try:
            stock_map = await ozon.fetch_stocks(stock_ids)
        except OzonError as e:
            logger.warning("Daily report stock refresh failed: %s", e)
    zero_stock = []
    for pid_key, meta in list(tracked.items()):
        pid = None
        try:
            pid = int(pid_key)
        except (TypeError, ValueError):
            try:
                pid = int(meta.get("product_id"))
            except (TypeError, ValueError):
                pid = None
        st = stock_map.get(pid, {}) if pid is not None else {}
        fbo = st.get("fbo", meta.get("fbo"))
        fbs = st.get("fbs", meta.get("fbs"))
        meta["fbo"] = fbo
        meta["fbs"] = fbs
        if fbo == 0 or fbs == 0:
            zero_stock.append(
                (
                    meta.get("name") or f"Товар {meta.get('product_id')}",
                    meta.get("offer_id"),
                    fbo,
                    fbs,
                )
            )
    state["tracked"] = tracked

    returns_list: List[Dict[str, Any]] = []
    try:
        for r in await ozon.fetch_returns(limit=100):
            dt = (r.get("logistic", {}) or {}).get("return_date") if isinstance(r.get("logistic"), dict) else None
            parsed = None
            if isinstance(dt, str):
                try:
                    parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
            if parsed is not None and parsed >= since_dt:
                returns_list.append(r)
        returns_list.sort(
            key=lambda r: (r.get("logistic", {}) or {}).get("return_date") or "",
            reverse=True,
        )
    except Exception as e:
        logger.warning("daily report returns failed: %s", e)

    lines = [
        f"📊 Ежедневный отчёт ({datetime.now().strftime('%d.%m.%Y')})",
        f"Заказов за 24ч: {count}",
        f"Выручка по заказам: {fmt_money(revenue)} ₽",
        f"Товаров с нулевым остатком: {len(zero_stock)}",
        f"Возвратов за 24ч: {len(returns_list)}",
    ]
    if top:
        lines.append("")
        lines.append("Топ по выручке:")
        for name, agg in top:
            lines.append(f"• {html_escape(str(name)[:45])} — {agg['qty']} шт, {fmt_money(agg['rev'])} ₽")

    lines.append("")
    if posts:
        lines.append(f"🛒 Заказы Ozon за 24ч ({count}):")
        sorted_posts = sorted(
            posts,
            key=lambda p: p.get("created_at") or p.get("in_process_at") or "",
            reverse=True,
        )
        for p in sorted_posts[:15]:
            pn = p.get("posting_number") or "—"
            scheme = (p.get("delivery_scheme") or "").upper() or ("FBO" if p.get("storage_type") is not None or not p.get("delivery_method") else "FBS")
            status_raw = p.get("status") or p.get("substatus") or ""
            status = ORDER_STATUS_LABELS.get(str(status_raw).lower(), str(status_raw))
            created = fmt_date(p.get("created_at") or p.get("in_process_at"))
            lines.append(f"• {scheme} {pn} — {status}")
            lines.append(f"  Создан: {created}")
            products = p.get("products", []) or []
            for prod in products[:3]:
                if not isinstance(prod, dict):
                    continue
                name = html_escape(str(prod.get("name") or prod.get("offer_id") or "товар")[:60])
                qty = prod.get("quantity") or 1
                pr = prod.get("price", {})
                amount = pr.get("amount") if isinstance(pr, dict) else None
                lines.append(f"  {name} × {qty}" + (f" — {fmt_money(amount)} ₽" if amount is not None else ""))
            if len(products) > 3:
                lines.append(f"  … и ещё {len(products) - 3} поз.")
        if count > 15:
            lines.append(f"… и ещё {count - 15} заказов")
    else:
        lines.append("🛒 Заказов за 24ч нет.")

    lines.append("")
    if zero_stock:
        lines.append(f"📦 Нулевые остатки Ozon ({len(zero_stock)}):")
        for name, offer, fbo, fbs in zero_stock[:15]:
            label = html_escape(str(name)[:70])
            if offer:
                label += f" ({html_escape(str(offer))})"
            fbo_s = str(fbo) if fbo is not None else "—"
            fbs_s = str(fbs) if fbs is not None else "—"
            lines.append(f"• {label} — FBO: {fbo_s}, FBS: {fbs_s}")
        if len(zero_stock) > 15:
            lines.append(f"… и ещё {len(zero_stock) - 15} товаров")
    else:
        lines.append("📦 Нулевых остатков нет.")

    lines.append("")
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            ym_orders = await ym_client.fetch_orders(limit=100)
            ym_recent: List[Dict[str, Any]] = []
            for order in ym_orders:
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                dt = None
                if isinstance(created, str):
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = None
                if dt is None or dt >= since_dt:
                    ym_recent.append(order)
            counts: Dict[str, int] = {}
            for offer in ym_offers:
                st = str(offer.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            low = [o for o in ym_offers if str(o.get("status") or "").upper() in ("NO_STOCKS", "DISABLED_BY_PARTNER") or (isinstance(o.get("stock"), int) and int(o.get("stock") or 0) <= 0)]
            lines.append("")
            lines.append(f"🟡 Яндекс Маркет: офферов {len(ym_offers)}, заказов за 24ч {len(ym_recent)}")
            if counts:
                lines.append("Статусы YM: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if low:
                lines.append(f"Офферы без остатка/выключенные YM: {len(low)}")
                for o in low[:8]:
                    lines.append(f"• {str(o.get('name') or o.get('offer_id'))[:60]} — статус {o.get('status')}, остаток {o.get('stock') if o.get('stock') is not None else '—'}")
        except Exception as e:
            logger.warning("YM daily report failed: %s", e)
            lines.append(f"🟡 Яндекс Маркет недоступен: {e}")

    if returns_list:
        lines.append(f"↩️ Возвраты за 24ч ({len(returns_list)}):")
        for r in returns_list[:10]:
            prod = r.get("product", {}) if isinstance(r.get("product"), dict) else {}
            name = html_escape(str(prod.get("name") or prod.get("offer_id") or "товар")[:60])
            offer = prod.get("offer_id")
            if offer:
                name += f" ({html_escape(str(offer))})"
            rid = r.get("id")
            schema = r.get("schema")
            rtype = r.get("type")
            reason = html_escape(str(r.get("return_reason_name") or "—")[:100])
            qty = prod.get("quantity")
            order = r.get("order_number") or "—"
            status = ""
            vis = r.get("visual", {})
            if isinstance(vis, dict) and isinstance(vis.get("status"), dict):
                status = vis["status"].get("display_name") or vis["status"].get("sys_name") or ""
            date = fmt_date((r.get("logistic", {}) or {}).get("return_date") if isinstance(r.get("logistic"), dict) else None)
            lines.append(f"• #{rid} — {name}")
            lines.append(f"  {qty} шт, {schema}/{rtype}, статус: {status or '—'}")
            lines.append(f"  Причина: {reason}")
            lines.append(f"  Заказ: {order}, {date}")
        if len(returns_list) > 10:
            lines.append(f"… и ещё {len(returns_list) - 10} возвратов")
    else:
        lines.append("↩️ Возвратов за 24ч нет.")

    await send_to_admins(application, "\n".join(lines))

DELIVERY_PERIODS = [(7, "7 дней"), (14, "2 недели"), (21, "3 недели"), (30, "месяц")]

DELIVERY_CONFIG_HINTS = {
    "ozon": "OZON_CLIENT_ID и OZON_API_KEY",
    "yandex": "YANDEX_DELIVERY_TOKEN",
    "cdek": "CDEK_ACCOUNT и CDEK_SECURE_PASSWORD",
    "yandex_market": "YANDEX_MARKET_API_KEY",
}


def delivery_period_label(days: int) -> str:
    for value, label in DELIVERY_PERIODS:
        if value == days:
            return label
    return f"{days} дн."


def delivery_providers_kb() -> InlineKeyboardMarkup:
    rows = []
    for provider in shipping.list():
        mark = "" if provider.configured() else " ⚠️"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{provider.emoji} {provider.title}{mark}",
                    callback_data=f"ship_prov:{provider.key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="help_now")])
    return InlineKeyboardMarkup(rows)


def delivery_periods_kb(provider_key: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for value, label in DELIVERY_PERIODS:
        row.append(
            InlineKeyboardButton(
                label,
                callback_data=f"ship_run:{provider_key}:{value}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="ship_menu"),
            InlineKeyboardButton("🏠 Меню", callback_data="help_now"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def delivery_report_kb(provider_key: str, days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"ship_run:{provider_key}:{days}")],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data=f"ship_prov:{provider_key}"),
                InlineKeyboardButton("🏠 Меню", callback_data="help_now"),
            ],
        ]
    )


def delivery_providers_text() -> str:
    lines = ["🚚 Доставки", "Выберите службу доставки:"]
    for provider in shipping.list():
        state_text = "настроена" if provider.configured() else "не настроена"
        lines.append(f"{provider.emoji} {provider.title} — {state_text}")
    lines.append("")
    lines.append("После выбора службы можно выбрать период: неделя, 2 недели, 3 недели или месяц.")
    return "\n".join(lines)


def delivery_periods_text(provider_key: str) -> str:
    provider = shipping.get(provider_key)
    if provider is None:
        return "Неизвестная служба доставки."
    lines = [f"{provider.emoji} {provider.title}", "Выберите период:"]
    if not provider.configured():
        hint = DELIVERY_CONFIG_HINTS.get(provider_key, "параметры в .env")
        lines.append("")
        lines.append(f"⚠️ Служба не настроена. Нужны: {hint}.")
    return "\n".join(lines)


async def _show_inline_message(message, text: str, kb: InlineKeyboardMarkup, edit: bool = False) -> None:
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=kb)


async def show_delivery_providers(message, edit: bool = False) -> None:
    await _show_inline_message(message, delivery_providers_text(), delivery_providers_kb(), edit=edit)


async def show_delivery_periods(message, provider_key: str, edit: bool = False) -> None:
    await _show_inline_message(
        message,
        delivery_periods_text(provider_key),
        delivery_periods_kb(provider_key),
        edit=edit,
    )


def _delivery_warning_counts(items, now: datetime, warn_hours: int):
    pickup_recent = []
    expiring = []
    for item in items:
        pickup_dt = parse_dt(item.pickup_ready_at)
        if pickup_dt and pickup_dt >= now - timedelta(hours=24):
            pickup_recent.append(item)
        exp_dt = parse_dt(item.storage_expires_at)
        if exp_dt and not item.is_delivered and not item.is_cancelled and exp_dt <= now + timedelta(hours=warn_hours):
            expiring.append(item)
    return pickup_recent, expiring


async def build_delivery_report(provider_key: str, days: int) -> tuple[str, InlineKeyboardMarkup]:
    provider = shipping.get(provider_key)
    if provider is None:
        return "Неизвестная служба доставки.", delivery_providers_kb()
    if not provider.configured():
        hint = DELIVERY_CONFIG_HINTS.get(provider_key, "параметры в .env")
        return (
            f"{provider.emoji} {provider.title}\n\n"
            f"⚠️ Служба не настроена.\nДобавьте в .env: {hint}.\n"
            "После изменения .env перезапустите бот.",
            delivery_periods_kb(provider_key),
        )

    state = await load_state()
    warn_hours = int(state.get("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS) or DELIVERY_STORAGE_WARN_HOURS)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    try:
        items = await shipping.fetch(provider_key, since, now, limit=300)
    except ShippingError as e:
        return f"❌ {provider.title}: {e}", delivery_report_kb(provider_key, days)
    except Exception as e:
        logger.warning("Delivery report failed for %s: %s", provider_key, e)
        return f"❌ {provider.title}: ошибка получения данных: {e}", delivery_report_kb(provider_key, days)

    delivered = [s for s in items if s.is_delivered]
    transit = [s for s in items if s.is_transit]
    cancelled = [s for s in items if s.is_cancelled and not s.is_delivered]
    transit.sort(key=lambda s: parse_dt(s.expected_at) or parse_dt(s.created_at) or datetime.min.replace(tzinfo=timezone.utc))
    delivered.sort(key=lambda s: parse_dt(s.delivered_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    cancelled.sort(key=lambda s: parse_dt(s.created_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    pickup_recent, expiring = _delivery_warning_counts(items, now, warn_hours)

    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            ym_orders = await ym_client.fetch_orders(limit=100)
            ym_recent: List[Dict[str, Any]] = []
            for order in ym_orders:
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                dt = None
                if isinstance(created, str):
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        dt = None
                if dt is None or dt >= since_dt:
                    ym_recent.append(order)
            counts: Dict[str, int] = {}
            for offer in ym_offers:
                st = str(offer.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            low = [o for o in ym_offers if str(o.get("status") or "").upper() in ("NO_STOCKS", "DISABLED_BY_PARTNER") or (isinstance(o.get("stock"), int) and int(o.get("stock") or 0) <= 0)]
            lines.append("")
            lines.append(f"🟡 Яндекс Маркет: офферов {len(ym_offers)}, заказов за 24ч {len(ym_recent)}")
            if counts:
                lines.append("Статусы YM: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
            if low:
                lines.append(f"Офферы без остатка/выключенные YM: {len(low)}")
                for o in low[:8]:
                    lines.append(f"• {str(o.get('name') or o.get('offer_id'))[:60]} — статус {o.get('status')}, остаток {o.get('stock') if o.get('stock') is not None else '—'}")
        except Exception as e:
            logger.warning("YM daily report failed: %s", e)
            lines.append(f"🟡 Яндекс Маркет недоступен: {e}")
    lines = [
        f"{provider.emoji} {provider.title} — {delivery_period_label(days)}",
        f"Всего: {len(items)} (в пути {len(transit)}, доставлено {len(delivered)}"
        + (f", отменено/возврат {len(cancelled)}" if cancelled else "")
        + ")",
    ]
    if pickup_recent:
        lines.append(f"⚠️ За последние 24 ч в ПВЗ: {len(pickup_recent)}")
    if expiring:
        lines.append(f"⏰ Истекает срок хранения (≤ {warn_hours} ч): {len(expiring)}")

    if not items:
        lines.append("")
        lines.append("За выбранный период ничего не найдено.")
        return "\n".join(lines), delivery_report_kb(provider_key, days)

    if transit:
        lines.append("")
        lines.append(f"🛣 В пути ({len(transit)}):")
        for s in transit[:10]:
            lines.append("")
            lines.append(f"• {s.tracking or s.shipment_id} — {s.status}")
            if s.product:
                lines.append(f"  Товар: {short_text(s.product, 80)}" + (f" × {s.quantity}" if s.quantity else ""))
            if s.destination:
                lines.append(f"  Куда: {short_text(s.destination, 100)}")
            extra = s.extra or {}
            if extra.get("ym_tracking") or s.tracking:
                track = extra.get("ym_tracking") or s.tracking
                if track and track != s.shipment_id:
                    lines.append(f"  Трек: {short_text(track, 100)}")
            recipient = extra.get("recipient_name") or extra.get("recipient")
            if isinstance(recipient, dict):
                recipient = recipient.get("recipient") or recipient.get("lastName")
            if recipient:
                lines.append(f"  Получатель: {short_text(recipient, 120)}")
            if extra.get("ym_delivery_window"):
                lines.append(f"  Интервал доставки: {short_text(extra.get('ym_delivery_window'), 70)}")
            if s.extra.get("pvz_address") and s.extra.get("pvz_address") not in str(s.destination or ""):
                lines.append(f"  ПВЗ: {short_text(s.extra.get('pvz_address'), 120)}")
            if s.extra.get("pvz_phone"):
                lines.append(f"  Телефон ПВЗ: {short_text(s.extra.get('pvz_phone'), 40)}")
            if s.extra.get("recipient_name"):
                recipient = s.extra.get("recipient_name")
                if s.extra.get("recipient_phone"):
                    recipient += f", {s.extra.get('recipient_phone')}"
                lines.append(f"  Получатель: {short_text(recipient, 120)}")
            if s.pickup_ready_at:
                lines.append(f"  📍 В ПВЗ: {fmt_date(s.pickup_ready_at)}")
                if s.storage_expires_at:
                    note = " (ориентировочно)" if s.extra.get("storage_estimated") else ""
                    lines.append(f"  ⏰ Хранение до: {fmt_date(s.storage_expires_at)}{note}")
            elif s.expected_at:
                lines.append(f"  Ожидается: {fmt_delivery_date(s.expected_at)}")
            if s.created_at:
                lines.append(f"  Создан: {fmt_date(s.created_at)}")
        if len(transit) > 10:
            lines.append(f"… и ещё {len(transit) - 10} в пути")

    if delivered:
        lines.append("")
        lines.append(f"✅ Доставлено ({len(delivered)}):")
        for s in delivered[:8]:
            lines.append("")
            lines.append(f"• {s.tracking or s.shipment_id} — {s.status}")
            if s.product:
                lines.append(f"  Товар: {short_text(s.product, 80)}" + (f" × {s.quantity}" if s.quantity else ""))
            if s.destination:
                lines.append(f"  Куда: {short_text(s.destination, 100)}")
            extra = s.extra or {}
            if extra.get("ym_tracking") or s.tracking:
                track = extra.get("ym_tracking") or s.tracking
                if track and track != s.shipment_id:
                    lines.append(f"  Трек: {short_text(track, 100)}")
            recipient = extra.get("recipient_name") or extra.get("recipient")
            if isinstance(recipient, dict):
                recipient = recipient.get("recipient") or recipient.get("lastName")
            if recipient:
                lines.append(f"  Получатель: {short_text(recipient, 120)}")
            if extra.get("ym_delivery_window"):
                lines.append(f"  Интервал доставки: {short_text(extra.get('ym_delivery_window'), 70)}")
            if s.extra.get("pvz_address") and s.extra.get("pvz_address") not in str(s.destination or ""):
                lines.append(f"  ПВЗ: {short_text(s.extra.get('pvz_address'), 120)}")
            if s.extra.get("pvz_phone"):
                lines.append(f"  Телефон ПВЗ: {short_text(s.extra.get('pvz_phone'), 40)}")
            if s.extra.get("recipient_name"):
                recipient = s.extra.get("recipient_name")
                if s.extra.get("recipient_phone"):
                    recipient += f", {s.extra.get('recipient_phone')}"
                lines.append(f"  Получатель: {short_text(recipient, 120)}")
            lines.append(f"  Доставлен: {fmt_date(s.delivered_at) if s.delivered_at else '—'}")
        if len(delivered) > 8:
            lines.append(f"… и ещё {len(delivered) - 8} доставлено")

    if cancelled:
        lines.append("")
        lines.append(f"↩️ Отменено/возврат ({len(cancelled)}):")
        for s in cancelled[:5]:
            lines.append(f"• {s.tracking or s.shipment_id} — {s.status}")
            if s.destination:
                lines.append(f"  Куда: {short_text(s.destination, 100)}")
            extra = s.extra or {}
            if extra.get("ym_tracking") or s.tracking:
                track = extra.get("ym_tracking") or s.tracking
                if track and track != s.shipment_id:
                    lines.append(f"  Трек: {short_text(track, 100)}")
            recipient = extra.get("recipient_name") or extra.get("recipient")
            if isinstance(recipient, dict):
                recipient = recipient.get("recipient") or recipient.get("lastName")
            if recipient:
                lines.append(f"  Получатель: {short_text(recipient, 120)}")
            if extra.get("ym_delivery_window"):
                lines.append(f"  Интервал доставки: {short_text(extra.get('ym_delivery_window'), 70)}")
        if len(cancelled) > 5:
            lines.append(f"… и ещё {len(cancelled) - 5}")

    return "\n".join(lines), delivery_report_kb(provider_key, days)


async def show_delivery_report(message, provider_key: str, days: int, edit: bool = False) -> None:
    text, kb = await build_delivery_report(provider_key, days)
    await _show_inline_message(message, text, kb, edit=edit)


def format_delivery_alert(shipment, events: List[str], warn_hours: int) -> str:
    provider = shipping.get(shipment.provider)
    title = provider.title if provider else shipment.provider
    emoji = provider.emoji if provider else "🚚"
    lines = []
    labels = []
    if "pickup" in events:
        labels.append("посылка пришла в ПВЗ")
    if "storage" in events:
        labels.append("истекает срок хранения")
    if "courier" in events:
        labels.append("выдана курьеру")
    if "delivered" in events:
        labels.append("доставлена")
    if labels:
        lines.append(f"📦 {emoji} {html_escape(title)}: " + ", ".join(labels))
    lines.append(f"№ {html_escape(str(shipment.tracking or shipment.shipment_id))}")
    if shipment.product:
        lines.append(f"Товар: {html_escape(short_text(shipment.product, 90))}" + (f" × {shipment.quantity}" if shipment.quantity else ""))
    if shipment.pickup_point or shipment.destination:
        lines.append(f"ПВЗ/куда: {html_escape(short_text(shipment.pickup_point or shipment.destination, 120))}")
    if shipment.pickup_ready_at:
        lines.append(f"Прибыла: {html_escape(fmt_date(shipment.pickup_ready_at))}")
    exp_dt = parse_dt(shipment.storage_expires_at)
    if exp_dt:
        note = " (ориентировочно)" if shipment.extra.get("storage_estimated") else ""
        lines.append(f"Срок хранения до: {html_escape(fmt_date(shipment.storage_expires_at))}{note}")
        left = exp_dt - datetime.now(timezone.utc)
        if left.total_seconds() < 0:
            lines.append("⚠️ Срок хранения уже истёк.")
        elif left.days >= 1:
            lines.append(f"Осталось: {left.days} дн.")
        else:
            lines.append(f"Осталось: {max(1, int(left.total_seconds() // 3600))} ч.")
    lines.append(f"Статус: {html_escape(shipment.status)}")
    return "\n".join(lines)


async def check_delivery_alerts(application: Application) -> None:
    state = await load_state()
    warn_hours = int(state.get("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS) or DELIVERY_STORAGE_WARN_HOURS)
    now = datetime.now(timezone.utc)
    try:
        items = await shipping.fetch_all(now - timedelta(days=14), now, limit=300)
    except Exception as e:
        logger.warning("Delivery alerts fetch failed: %s", e)
        return
    if not items:
        return
    alerted = state.setdefault("delivery_alerts", {})
    changed = False
    today_key = datetime.now().astimezone().date().isoformat()
    for shipment in items:
        if shipment.is_cancelled:
            continue
        events: List[str] = []
        is_delivered = shipment.is_delivered
        pickup_dt = parse_dt(shipment.pickup_ready_at)
        if (not is_delivered) and pickup_dt and pickup_dt >= now - timedelta(hours=24):
            key = f"{shipment.key}:pickup"
            if key not in alerted:
                events.append("pickup")
        exp_dt = parse_dt(shipment.storage_expires_at)
        if (not is_delivered) and exp_dt and exp_dt <= now + timedelta(hours=warn_hours):
            key = f"{shipment.key}:storage:{exp_dt.date().isoformat()}:{today_key}"
            if key not in alerted:
                events.append("storage")
        # A new courier-handoff status is useful as a push notification. Keep
        # this limited to providers/statuses where it means handover to courier.
        status_time = parse_dt((shipment.extra or {}).get("status_time") or (shipment.extra or {}).get("yandex_status_time") or shipment.created_at)
        status_raw = str(shipment.status_code or "").upper()
        courier_statuses = {
            "DELIVERY_TRANSPORTATION_RECIPIENT",
            "PERFORMER_FOUND",
            "DELIVERY_TRANSPORTATION_RECIPIENT",
            "DELIVERY",
            "TRANSPORTATION",
        }
        if (not is_delivered) and status_raw in courier_statuses and status_time is not None and status_time >= now - timedelta(hours=24):
            key = f"{shipment.key}:courier"
            if key not in alerted:
                events.append("courier")
        delivered_dt = parse_dt(shipment.delivered_at)
        if is_delivered and delivered_dt is not None and delivered_dt >= now - timedelta(hours=24):
            key = f"{shipment.key}:delivered"
            if key not in alerted:
                events.append("delivered")
        if not events:
            continue
        await send_to_admins(application, format_delivery_alert(shipment, events, warn_hours))
        for event in events:
            if event == "pickup":
                alerted[f"{shipment.key}:pickup"] = now.isoformat()
            elif event == "storage" and exp_dt is not None:
                alerted[f"{shipment.key}:storage:{exp_dt.date().isoformat()}:{today_key}"] = now.isoformat()
            elif event == "courier":
                alerted[f"{shipment.key}:courier"] = now.isoformat()
            elif event == "delivered":
                alerted[f"{shipment.key}:delivered"] = now.isoformat()
        changed = True
    cutoff = (now - timedelta(days=60)).isoformat()
    for key in list(alerted.keys()):
        if str(alerted[key]) < cutoff:
            del alerted[key]
            changed = True
    if changed:
        await storage.save(state)


async def periodic_loop(application: Application) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            st = await load_state()
        except Exception:
            st = {}
        if st.get("monitoring_enabled", True):
            try:
                await run_price_check(application)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Periodic check failed: %s", e)
            try:
                await check_delivery_alerts(application)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Periodic delivery alerts failed: %s", e)
            try:
                st = await load_state()
                now_local = datetime.now()
                today = now_local.date().isoformat()
                report_hour = int(st.get("report_hour_local", 10))
                # catch-up window: report hour and the next 5 hours
                due = report_hour <= now_local.hour < report_hour + 6
                if st.get("last_daily_report_date") != today and due:
                    await send_daily_report(application)
                    st = await load_state()
                    st["last_daily_report_date"] = today
                    await storage.save(st)
            except Exception as e:
                logger.warning("daily report failed: %s", e)
        else:
            logger.info("Monitoring disabled; skipping automatic check")
        try:
            st = await load_state()
            interval = int(st.get("check_interval") or CHECK_INTERVAL_SEC)
        except Exception:
            interval = CHECK_INTERVAL_SEC
        await asyncio.sleep(max(60, interval))

async def post_init(application: Application) -> None:
    await load_state()
    task = asyncio.create_task(periodic_loop(application))
    application.bot_data["background_task"] = task


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("background_task")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

# ---------- commands ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    state = await load_state()
    cid = chat.id
    if not state.get("admins"):
        state["admins"].append(cid)
        await storage.save(state)
        await update.message.reply_text(
            "Вы зарегистрированы как владелец бота.\n"
            "Команды: /help\n"
            "Сначала выполните /sync, чтобы загрузить товары из Ozon.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text("Главное меню (inline-кнопки, работают везде):", reply_markup=inline_main_kb())
        return
    if cid not in state.get("admins", []):
        await update.message.reply_text("Доступ запрещён.")
        return
    monitoring = "включён" if state.get("monitoring_enabled", True) else "выключен"
    await update.message.reply_text(
        "Бот отслеживания цен Ozon активен.\n"
        f"Товаров: {len(state.get('tracked') or {})}\n"
        f"Порог: {fmt_money(state.get('threshold', THRESHOLD_RUB))} ₽\n"
        f"Мониторинг: {monitoring}\n"
        "Команды: /help",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Главное меню:", reply_markup=inline_main_kb())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    await update.message.reply_text(
        "<b>Команды</b>\n"
        "/sync — загрузить все товары из Ozon (цены/остатки) и обновить базу\n"
        "/check — проверить цены/остатки/возвраты сейчас\n"
        "/list [N] — показать отслеживаемые товары\n"
        "/prices — показать все доступные цены Ozon API\n"
        "/index — показать ценовые индексы Ozon (конкуренты)\n"
        "/setinterval 15 — интервал проверки в минутах\n"
        "/orders [N] — последние заказы FBS/FBO\n"
        "/deliveries — доставки Ozon / Яндекс / СДЭК (выбор службы и периода)\n"
        "/deliveries ozon 7 — пример отчёта по доставкам\n"
        "/deliveries yandex 14\n"
        "/deliveries cdek 30\n"
        "/ym — офферы Яндекс Маркет (цены, статусы, склад, заказы)\n"
        "/returnstats — сводка возвратов по статусам\n"
        "/reviews [N] — последние отзывы\n"
        "/stock [N] — показать остатки FBO/FBS\n"
        "/returns [N] — показать последние возвраты\n"
        "/add &lt;product_id&gt; — добавить товар вручную\n"
        "/remove &lt;product_id&gt; — удалить товар\n"
        "/setthreshold &lt;рубли&gt; — порог изменения цены\n"
        "/setstockthreshold &lt;шт&gt; — порог остатков\n"
        "/setordersdays &lt;1-7&gt; — окно заказов в днях\n"
        "/report — отправить ежедневный отчёт сейчас\n"
        "/status — статус\n"
        "/admins — список админов\n"
        "/help — эта справка",
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("Главное меню:", reply_markup=inline_main_kb())

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    msg = await update.message.reply_text("⏳ Синхронизирую товары из Ozon…")
    try:
        refs = await ozon.list_products()
    except OzonError as e:
        await msg.edit_text(f"❌ Ошибка получения списка товаров: {e}")
        return
    try:
        info = await ozon.get_product_info([r["product_id"] for r in refs])
    except OzonError as e:
        info = {}
        logger.warning("product info failed: %s", e)
    try:
        prices = await ozon.fetch_prices()
    except OzonError as e:
        await msg.edit_text(f"❌ Ошибка получения цен: {e}")
        return
    price_map = {}
    for it in prices:
        pid = it.get("product_id")
        if pid is None:
            continue
        try:
            price_map[int(pid)] = it
        except (ValueError, TypeError):
            continue
    try:
        stock_map = await ozon.fetch_stocks([r["product_id"] for r in refs])
    except OzonError as e:
        stock_map = {}
        logger.warning("stock fetch failed: %s", e)
    try:
        returns_list = await ozon.fetch_returns(limit=100)
    except OzonError as e:
        returns_list = []
        logger.warning("returns fetch failed: %s", e)

    state = await load_state()
    tracked = state.get("tracked") or {}
    new_count = 0
    updated = 0
    for ref in refs:
        pid = ref["product_id"]
        offer_id = ref.get("offer_id")
        meta_info = info.get(pid, {})
        if meta_info.get("offer_id"):
            offer_id = meta_info.get("offer_id")
        name = meta_info.get("name")
        price_item = price_map.get(pid)
        current_price = price_item.get("price") if price_item else None
        try:
            current_price = float(current_price) if current_price is not None else None
        except (TypeError, ValueError):
            current_price = None
        if price_item and price_item.get("offer_id"):
            offer_id = price_item.get("offer_id")
        st = stock_map.get(pid, {})
        if st.get("offer_id"):
            offer_id = st.get("offer_id") or offer_id

        existing = tracked.get(str(pid))
        if existing is None:
            tracked[str(pid)] = {
                "product_id": pid,
                "offer_id": offer_id,
                "name": name,
                "last_price": current_price,
                "fbo": st.get("fbo"),
                "fbs": st.get("fbs"),
                "stock_initialized": True,
                "last_checked": now_iso() if current_price is not None else None,
                "last_status": "ok" if current_price is not None else "no_price",
            }
            new_count += 1
        else:
            existing["offer_id"] = offer_id or existing.get("offer_id")
            if name:
                existing["name"] = name
            if existing.get("last_price") is None and current_price is not None:
                existing["last_price"] = current_price
            existing["fbo"] = st.get("fbo") if st else existing.get("fbo")
            existing["fbs"] = st.get("fbs") if st else existing.get("fbs")
            existing["stock_initialized"] = True
            existing["last_checked"] = now_iso() if current_price is not None else existing.get("last_checked")
            if current_price is not None:
                existing["last_status"] = "ok"
            tracked[str(pid)] = existing
            updated += 1

    ym_offer_count = 0
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers_list = await ym_client.fetch_offers()
            state["ym_offers"] = {str(o.get("offer_id")): {"name": o.get("name"), "price": o.get("basic_price"), "status": o.get("status"), "stock": o.get("stock")} for o in ym_offers_list if o.get("offer_id") is not None}
            state["ym_offers_initialized"] = bool(ym_offers_list)
            ym_offer_count = len(ym_offers_list)
        except Exception as e:
            await send_to_admins(application, f"⚠️ Ошибка Yandex Market (синхронизация): {e}")
    ref_ids = {r["product_id"] for r in refs}
    removed = [pid for pid in list(tracked.keys()) if int(pid) not in ref_ids]
    for pid in removed:
        tracked[pid]["last_status"] = "removed_from_ozon"

    state["tracked"] = tracked
    # baseline returns
    seen = set(state.get("seen_return_ids") or [])
    for r in returns_list:
        rid = r.get("id")
        if rid is not None:
            seen.add(rid)
    state["seen_return_ids"] = list(seen)[-5000:]
    state["returns_initialized"] = True
    await storage.save(state)

    await msg.edit_text(
        f"✅ Синхронизация завершена.\n"
        f"Товаров в Ozon: {len(refs)}\n"
        f"Новых добавлено: {new_count}\n"
        f"Обновлено: {updated}\n"
        f"Не найдено в refs: {len(removed)}\n"
        f"Цен получено: {len(price_map)}\n"
        f"Остатков получено: {len(stock_map)}\n"
        f"Возвратов просмотрено: {len(returns_list)}\n"
        + (f"Офферов Яндекс Маркет: {ym_offer_count}" if ym_offer_count else ""),
        reply_markup=inline_main_kb(),
    )
    await update.message.reply_text("Главное меню:", reply_markup=inline_main_kb())

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    await update.message.reply_text("⏳ Проверяю цены/остатки/возвраты и доставки…")
    await run_price_check(context.application)
    try:
        await check_delivery_alerts(context.application)
    except Exception as e:
        logger.warning("Delivery check failed: %s", e)
    await update.message.reply_text("✅ Проверка завершена. Уведомления отправлены при изменениях.", reply_markup=inline_main_kb())

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    tracked = state.get("tracked") or {}
    if not tracked:
        await update.message.reply_text("Список пуст. Выполните /sync.", reply_markup=ReplyKeyboardRemove())
        return
    await show_list_view(update.message, 0, edit=False)

async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    tracked = state.get("tracked") or {}
    if not tracked:
        await update.message.reply_text("Список пуст. Выполните /sync.")
        return
    try:
        n = int(context.args[0]) if context.args else 20
    except (ValueError, IndexError):
        n = 20
    n = max(1, min(n, 50))
    shown = list(tracked.items())[:n]
    stock_map = {}
    try:
        stock_map = await ozon.fetch_stocks([int(pid) for pid, _ in shown])
    except Exception as e:
        logger.warning("Stock display fetch failed: %s", e)
    lines = [f"📦 Остатки Ozon ({len(tracked)}):"]
    for pid, meta in shown:
        name = meta.get("name") or f"Товар {pid}"
        st = stock_map.get(int(pid), {}) if str(pid).isdigit() else {}
        fbo = st.get("fbo", meta.get("fbo"))
        fbo_reserved = st.get("fbo_reserved")
        fbs = st.get("fbs", meta.get("fbs"))
        fbs_reserved = st.get("fbs_reserved")
        fbo_text = f"{fbo if fbo is not None else '—'} (резерв {fbo_reserved if fbo_reserved is not None else '—'})"
        fbs_text = f"{fbs if fbs is not None else '—'} (резерв {fbs_reserved if fbs_reserved is not None else '—'})"
        lines.append(f"• {name}: FBO {fbo_text}, FBS {fbs_text}")
    if len(tracked) > len(shown):
        lines.append(f"… и ещё {len(tracked) - len(shown)}")
    # Yandex Market stock available in the same screen, clearly labeled.
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            if ym_offers:
                lines.append("")
                lines.append("🟡 Яндекс Маркет:")
                for offer in ym_offers[: min(20, len(ym_offers))]:
                    lines.append(
                        f"• {str(offer.get('name') or offer.get('offer_id'))[:60]}: "
                        f"Остаток FIT {offer.get('stock') if offer.get('stock') is not None else '—'}, "
                        f"статус {offer.get('status') or '—'}, цена {fmt_money(offer.get('basic_price'))} ₽"
                    )
        except Exception as e:
            lines.append(f"YM stocks unavailable: {e}")
    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())

async def cmd_returns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    try:
        n = int(context.args[0]) if context.args else 5
    except (ValueError, IndexError):
        n = 5
    n = max(1, min(n, 20))
    lines = ["Возвраты (по 5 из каждого магазина):"]
    try:
        returns_list = await ozon.fetch_returns(limit=n)
    except OzonError as e:
        returns_list = []
        lines.append(f"❌ Ozon: {e}")
    if returns_list:
        returns_list = sorted(returns_list, key=lambda x: x.get("id", 0), reverse=True)[:n]
        lines.append("")
        lines.append(f"↩️ Возвраты Ozon ({len(returns_list)}):")
        for r in returns_list:
            prod = r.get("product", {}) if isinstance(r.get("product"), dict) else {}
            name = prod.get("name") or prod.get("offer_id") or "товар"
            if len(str(name)) > 60:
                name = str(name)[:58] + "…"
            offer = prod.get("offer_id")
            rid = r.get("id")
            schema = r.get("schema")
            rtype = r.get("type")
            reason = r.get("return_reason_name")
            qty = prod.get("quantity")
            order = r.get("order_number")
            status = ""
            vis = r.get("visual", {})
            if isinstance(vis, dict) and isinstance(vis.get("status"), dict):
                status = vis["status"].get("display_name") or vis["status"].get("sys_name") or ""
            date = r.get("logistic", {}).get("return_date") if isinstance(r.get("logistic"), dict) else None
            date = fmt_date(date)
            lines.append(
                f"\n📌 Возврат Ozon #{rid}\n"
                f"• Товар: {name}" + (f" ({offer})" if offer else "") + "\n"
                f"• Схема/тип: {schema} / {rtype}\n"
                f"• Кол-во: {qty} шт | Статус: {status}\n"
                f"• Причина: {reason}\n"
                f"• Заказ: {order}\n"
                f"• Дата: {date}"
            )
    else:
        if not lines[-1].startswith("❌ Ozon"):
            lines.append("")
            lines.append("↩️ Возвраты Ozon: нет возвратов за период")
    state = await load_state()
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_returns = await ym_client.fetch_returns(limit=n)
        except Exception as e:
            ym_returns = []
            lines.append(f"YM returns unavailable: {e}")
        if ym_returns:
            lines.append("")
            lines.append(f"↩️ Возвраты Яндекс Маркет ({len(ym_returns)}):")
            for r in ym_returns:
                rid = r.get("RETURN_NUMBER") or r.get("returnNumber") or r.get("return_number") or r.get("orderId") or r.get("ORDER_ID")
                offer = r.get("SHOP_SKU") or r.get("shopSku") or r.get("offerId") or r.get("offer_id")
                name = r.get("PRODUCT_NAME") or r.get("productName") or r.get("offerName") or r.get("name") or "товар"
                status = r.get("STATUS") or r.get("status") or ""
                qty = r.get("COUNT") or r.get("count") or r.get("COUNT_OF_FIT")
                date = r.get("DATE_OF_RETURN_CREATION") or r.get("dateOfReturnCreation") or r.get("RETURN_PICKED_DATE") or r.get("placementDate")
                lines.append(
                    f"\n📌 Возврат ЯМ #{rid}\n"
                    f"• Товар: {name}" + (f" ({offer})" if offer else "") + "\n"
                    f"• Кол-во: {qty} шт | Статус: {status}\n"
                    f"• Дата: {fmt_date(date)}"
                )
        else:
            lines.append("")
            lines.append("↩️ Возвраты Яндекс Маркет: нет возвратов за 30 дней")
    if len(lines) == 1:
        lines.append("Возвратов не найдено.")
    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /add <product_id>")
        return
    try:
        pid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("product_id должен быть числом.")
        return
    try:
        prices = await ozon.fetch_prices([pid])
    except OzonError as e:
        await update.message.reply_text(f"❌ Ошибка Ozon: {e}")
        return
    match = next((p for p in prices if p.get("product_id") == pid), None)
    current_price = None
    offer_id = None
    if match:
        current_price = match.get("price")
        try:
            current_price = float(current_price) if current_price is not None else None
        except (TypeError, ValueError):
            current_price = None
        offer_id = match.get("offer_id")
    name = None
    try:
        info = await ozon.get_product_info([pid])
        if pid in info:
            name = info[pid].get("name")
            offer_id = info[pid].get("offer_id") or offer_id
    except OzonError:
        pass
    # stock
    fbo = None
    fbs = None
    try:
        st = await ozon.fetch_stocks([pid])
        if pid in st:
            fbo = st[pid].get("fbo")
            fbs = st[pid].get("fbs")
            offer_id = st[pid].get("offer_id") or offer_id
    except OzonError:
        pass
    state = await load_state()
    tracked = state.get("tracked") or {}
    tracked[str(pid)] = {
        "product_id": pid,
        "offer_id": offer_id,
        "name": name,
        "last_price": current_price,
        "fbo": fbo,
        "fbs": fbs,
        "stock_initialized": True,
        "last_checked": now_iso() if current_price is not None else None,
        "last_status": "ok" if current_price is not None else "no_price",
    }
    state["tracked"] = tracked
    await storage.save(state)
    await update.message.reply_text(
        f"✅ Добавлен: {name or pid}\n"
        f"Цена: {fmt_money(current_price) if current_price is not None else '—'} ₽\n"
        f"Остатки: FBO={fbo if fbo is not None else '—'}, FBS={fbs if fbs is not None else '—'}"
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove <product_id>")
        return
    try:
        pid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("product_id должен быть числом.")
        return
    state = await load_state()
    tracked = state.get("tracked") or {}
    if str(pid) not in tracked:
        await update.message.reply_text("Товар не найден в списке.")
        return
    del tracked[str(pid)]
    state["tracked"] = tracked
    await storage.save(state)
    await update.message.reply_text(f"✅ Удалён product_id {pid}.")

async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    if not context.args:
        state = await load_state()
        await update.message.reply_text(f"Текущий порог: {fmt_money(state.get('threshold', THRESHOLD_RUB))} ₽")
        return
    try:
        val = float(context.args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Укажите число, например /setthreshold 50")
        return
    if val < 0:
        await update.message.reply_text("Порог не может быть отрицательным.")
        return
    state = await load_state()
    state["threshold"] = val
    await storage.save(state)
    await update.message.reply_text(f"✅ Порог изменён: {fmt_money(val)} ₽")

async def cmd_setstockthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    if not context.args:
        cur = state.get("stock_threshold", STOCK_ALERT_THRESHOLD)
        await update.message.reply_text(
            f"Текущий порог остатков: {cur} шт.\nИспользование: /setstockthreshold 0 (0–100)",
            reply_markup=inline_main_kb(),
        )
        return
    try:
        val = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Укажите целое число, например /setstockthreshold 0")
        return
    if not (0 <= val <= 100):
        await update.message.reply_text("Порог остатков должен быть от 0 до 100.")
        return
    state["stock_threshold"] = val
    await storage.save(state)
    await update.message.reply_text(f"✅ Порог остатков: {val} шт.", reply_markup=inline_main_kb())

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    tracked = state.get("tracked") or {}
    summary = state.get("last_check_summary")
    interval_min = int(state.get("check_interval", CHECK_INTERVAL_SEC)) // 60
    text = (
        f"Бот: {'активен' if state.get('monitoring_enabled', True) else 'на паузе'}\n"
        f"Мониторинг: {'включён' if state.get('monitoring_enabled', True) else 'выключен'}\n"
        f"Товаров: {len(tracked)}\n"
        f"Порог цены: {fmt_money(state.get('threshold', THRESHOLD_RUB))} ₽\n"
        f"Порог остатков: {state.get('stock_threshold', STOCK_ALERT_THRESHOLD)} шт\n"
        f"Интервал проверки: {interval_min} мин\n"
        f"Окно заказов: {state.get('orders_days', 3)} дн\n"
        f"Время отчёта: {state.get('report_hour_local', 10)}:00 (серверное)\n"
        f"Предупреждение о хранении: за {state.get('delivery_warn_hours', DELIVERY_STORAGE_WARN_HOURS)} ч\n"
        f"Ozon настроен: {'да' if ozon.configured() else 'нет'}\n"
        f"Яндекс Доставка: {'настроена' if shipping.get('yandex') and shipping.get('yandex').configured() else 'не настроена'}\n"
        f"СДЭК: {'настроен' if shipping.get('cdek') and shipping.get('cdek').configured() else 'не настроен'}\n"
        f"Серверное время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Europe/Helsinki)\n"
        f"Последняя проверка: {fmt_date(state.get('last_check')) if state.get('last_check') else '—'}\n"
    )
    if summary:
        text += (
            f"Итог: проверено {summary.get('checked')}, изменений цен {summary.get('price_changes')}, "
            f"алертов по остаткам {summary.get('stock_alerts')}, новых возвратов {summary.get('new_returns')}\n"
        )
    await update.message.reply_text(text, reply_markup=inline_main_kb())

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    ids = state.get("admins") or []
    await update.message.reply_text("Админы: " + (", ".join(map(str, ids)) if ids else "нет"))

# ---------- keyboard / callback helpers ----------

def settings_main_kb(state: Dict[str, Any]) -> InlineKeyboardMarkup:
    mon = bool(state.get("monitoring_enabled", True))
    stock_mon = bool(state.get("stock_monitoring_enabled", True))
    store_ozon_mon = bool(state.get("store_mon_ozon", True))
    store_ym_mon = bool(state.get("store_mon_ym", True))
    thr = fmt_money(state.get("threshold", THRESHOLD_RUB))
    interval = int(state.get("check_interval", CHECK_INTERVAL_SEC)) // 60
    report = state.get("report_hour_local", 10)
    orders = state.get("orders_days", 3)
    stock = state.get("stock_threshold", STOCK_ALERT_THRESHOLD)
    warn = state.get("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"💰 Порог цены: {thr} ₽", callback_data="set_page:price")],
            [InlineKeyboardButton(f"⏱ Интервал: {interval} мин", callback_data="set_page:interval")],
            [InlineKeyboardButton(f"🕒 Отчёт: {report}:00", callback_data="set_page:report")],
            [InlineKeyboardButton(f"🛒 Заказы: {orders} дн", callback_data="set_page:orders")],
            [InlineKeyboardButton(f"📦 Остатки: {stock} шт", callback_data="set_page:stock")],
            [InlineKeyboardButton(f"⏰ Хранение: {warn} ч", callback_data="set_page:storage")],
            [InlineKeyboardButton((("🟢 ⏸ Выключить мониторинг остатков" if stock_mon else "🔴 ▶️ Включить мониторинг остатков")), callback_data="stockmonitor_toggle")],
            [InlineKeyboardButton((("🟢 ⏸ Ozon мониторинг" if store_ozon_mon else "🔴 ▶️ Ozon мониторинг")), callback_data="storemonitor_ozon")],
            [InlineKeyboardButton((("🟢 ⏸ ЯМ мониторинг" if store_ym_mon else "🔴 ▶️ ЯМ мониторинг")), callback_data="storemonitor_ym")],
            [InlineKeyboardButton((("🟢 ⏸ Выключить мониторинг" if mon else "🔴 ▶️ Включить мониторинг")), callback_data="monitor_toggle")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="help_now")],
        ]
    )


def settings_page_kb(page: str, state: Dict[str, Any]) -> InlineKeyboardMarkup:
    if page == "price":
        title, values = "Порог изменения цены", [50, 100, 200, 500, 1000]
        cb, cur = "setthr", state.get("threshold", THRESHOLD_RUB)
    elif page == "interval":
        title, values = "Интервал проверки", [5, 10, 15, 30, 60]
        cb, cur = "setint", int(state.get("check_interval", CHECK_INTERVAL_SEC)) // 60
    elif page == "report":
        title, values = "Время ежедневного отчёта", [6, 7, 8, 9, 10, 11]
        cb, cur = "sethour", state.get("report_hour_local", 10)
    elif page == "orders":
        title, values = "Окно заказов", [1, 2, 3, 5, 7]
        cb, cur = "setdays", state.get("orders_days", 3)
    elif page == "stock":
        title, values = "Порог остатков", [0, 1, 2, 3, 5]
        cb, cur = "setstock", state.get("stock_threshold", STOCK_ALERT_THRESHOLD)
    elif page == "storage":
        title, values = "Предупреждать о сроке хранения", [6, 12, 24, 48, 72]
        cb, cur = "setwarn", state.get("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS)
    else:
        return settings_main_kb(state)
    rows = []
    row = []
    for v in values:
        mark = "✅ " if str(v) == str(cur) else ""
        row.append(InlineKeyboardButton(f"{mark}{v}", callback_data=f"{cb}:{v}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)


def settings_page_text(page: str, state: Dict[str, Any]) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    mon = "включён" if state.get("monitoring_enabled", True) else "выключен"
    stock_mon = "включён" if state.get("stock_monitoring_enabled", True) else "выключен"
    store_ozon_mon = "включён" if state.get("store_mon_ozon", True) else "выключен"
    store_ym_mon = "включён" if state.get("store_mon_ym", True) else "выключен"
    thr = fmt_money(state.get("threshold", THRESHOLD_RUB))
    interval = int(state.get("check_interval", CHECK_INTERVAL_SEC)) // 60
    report = state.get("report_hour_local", 10)
    orders = state.get("orders_days", 3)
    stock = state.get("stock_threshold", STOCK_ALERT_THRESHOLD)
    warn = state.get("delivery_warn_hours", DELIVERY_STORAGE_WARN_HOURS)
    if page == "price":
        return f"💰 Порог изменения цены\nСейчас: {thr} ₽\n\nВыберите значение:"
    if page == "interval":
        return f"⏱ Интервал проверки\nСейчас: {interval} мин\n\nВыберите значение:"
    if page == "report":
        return f"🕒 Время ежедневного отчёта\nСейчас: {report}:00 (серверное)\n\nВыберите час:"
    if page == "orders":
        return f"🛒 Окно заказов\nСейчас: {orders} дн\n\nВыберите значение:"
    if page == "stock":
        return f"📦 Порог остатков\nСейчас: {stock} шт\n\nВыберите значение:"
    if page == "storage":
        return (
            f"⏰ Предупреждение о сроке хранения\nСейчас: за {warn} ч до истечения\n\n"
            "Бот пришлёт сообщение, когда посылка в ПВЗ и срок хранения подходит к концу. "
            "Выберите, за сколько часов предупреждать:"
        )
    return (
        "⚙️ Настройки\n"
        f"Мониторинг: {mon}\n"
        f"Мониторинг остатков: {stock_mon}\n"
        f"Ozon мониторинг: {store_ozon_mon}\n"
        f"Яндекс Маркет мониторинг: {store_ym_mon}\n"
        f"Порог цены: {thr} ₽\n"
        f"Порог остатков: {stock} шт\n"
        f"Интервал проверки: {interval} мин\n"
        f"Окно заказов: {orders} дн\n"
        f"Время отчёта: {report}:00 (серверное)\n"
        f"Предупреждение о хранении: за {warn} ч\n"
        f"Серверное время: {now} (Europe/Helsinki, UTC+2)\n"
        f"Товаров: {len(state.get('tracked') or {})}\n\n"
        "Нажмите параметр, чтобы изменить его."
    )


async def show_settings_page(message, page: str = "main", edit: bool = False) -> None:
    state = await load_state()
    text = settings_page_text(page, state)
    kb = settings_main_kb(state) if page == "main" else settings_page_kb(page, state)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=kb)

async def build_list_view(offset: int):
    state = await load_state()
    tracked = list((state.get("tracked") or {}).items())
    total = len(tracked)
    if total == 0:
        return "Список пуст. Выполните /sync.", None
    if total <= PAGE_SIZE:
        offset = 0
    else:
        offset = max(0, min(offset, total - PAGE_SIZE))
    page = tracked[offset : offset + PAGE_SIZE]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    stock_map = {}
    try:
        stock_map = await ozon.fetch_stocks([int(pid) for pid, _ in page])
    except Exception as e:
        logger.debug("list stock fetch failed: %s", e)
    lines = [f"📋 Товары Ozon ({total}), страница {offset // PAGE_SIZE + 1}/{pages}:"]
    for i, (pid, meta) in enumerate(page, start=offset + 1):
        name = meta.get("name") or f"Товар {pid}"
        offer = meta.get("offer_id")
        price = meta.get("last_price")
        st = stock_map.get(int(pid), {}) if str(pid).isdigit() else {}
        fbo = st.get("fbo", meta.get("fbo"))
        fbo_reserved = st.get("fbo_reserved")
        fbs = st.get("fbs", meta.get("fbs"))
        fbs_reserved = st.get("fbs_reserved")
        stock_text = (
            f"FBO={fbo if fbo is not None else '—'}"
            + (f"(rez {fbo_reserved})" if fbo_reserved is not None else "")
            + f", FBS={fbs if fbs is not None else '—'}"
            + (f"(rez {fbs_reserved})" if fbs_reserved is not None else "")
        )
        line = f"{i}. {name}"
        if offer:
            line += f" ({offer})"
        line += f" — {fmt_money(price) if price is not None else '—'} ₽; {stock_text}"
        lines.append(line)
    buttons = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"listpage:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"listpage:{offset + PAGE_SIZE}"))
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=f"listpage:{offset}"),
            InlineKeyboardButton("🏠 Меню", callback_data="help_now"),
        ]
    )

    state = await load_state()
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            if ym_offers:
                lines.append("")
                lines.append(f"🟡 Яндекс Маркет ({len(ym_offers)}):")
                for offer in ym_offers[:8]:
                    stock_txt = f", остаток FIT {offer.get('stock')}" if offer.get("stock") is not None else ""
                    lines.append(f"• {str(offer.get('name') or offer.get('offer_id'))[:60]} — {offer.get('status') or '—'}, {fmt_money(offer.get('basic_price'))} ₽{stock_txt}")
        except Exception as e:
            lines.append(f"YM offers unavailable: {e}")

    kb = InlineKeyboardMarkup(buttons)
    return "\n".join(lines), kb

async def show_list_view(message, offset: int, edit: bool = False):
    text, kb = await build_list_view(offset)
    try:
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.reply_text(text, reply_markup=kb)
    except Exception as e:
        logger.debug("edit failed: %s", e)
        await message.reply_text(text, reply_markup=kb)

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    await show_settings_page(update.message, "main")
async def text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (update.message.text or "").strip()
    mapping = {
        "🔄 Синхронизировать": "sync",
        "✅ Проверить": "check",
        "📋 Товары": "list",
        "📦 Остатки": "stock",
        "↩️ Возвраты": "returns",
        "⚙️ Настройки": "settings",
        "❓ Помощь": "help",
    }
    if text not in mapping:
        return
    if text == "❓ Помощь":
        await cmd_help(update, context)
        return
    if text == "🔄 Синхронизировать":
        await cmd_sync(update, context)
    elif text == "✅ Проверить":
        await cmd_check(update, context)
    elif text == "📋 Товары":
        await cmd_list(update, context)
    elif text == "📦 Остатки":
        await cmd_stock(update, context)
    elif text == "↩️ Возвраты":
        await cmd_returns(update, context)
    elif text == "⚙️ Настройки":
        await cmd_settings(update, context)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    from types import SimpleNamespace

    if data == "noop":
        return
    if data == "ship_menu":
        await show_delivery_providers(query.message, edit=True)
        return
    if data.startswith("ship_prov:"):
        provider_key = data.split(":", 1)[1]
        await show_delivery_periods(query.message, provider_key, edit=True)
        return
    if data.startswith("ship_run:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        provider_key = parts[1]
        try:
            days = int(parts[2])
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 90))
        await show_delivery_report(query.message, provider_key, days, edit=True)
        return
    if data.startswith("listpage:"):
        try:
            offset = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            offset = 0
        await show_list_view(query.message, offset, edit=True)
        return
    if data.startswith("set_page:"):
        page = data.split(":", 1)[1]
        await show_settings_page(query.message, page, edit=True)
        return
    if data == "settings_back":
        await show_settings_page(query.message, "main", edit=True)
        return
    if data == "storemonitor_ozon":
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        state["store_mon_ozon"] = not bool(state.get("store_mon_ozon", True))
        await storage.save(state)
        await show_settings_page(query.message, "main", edit=True)
        return
    if data == "storemonitor_ym":
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        state["store_mon_ym"] = not bool(state.get("store_mon_ym", True))
        await storage.save(state)
        await show_settings_page(query.message, "main", edit=True)
        return
    if data == "stockmonitor_toggle":
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        state["stock_monitoring_enabled"] = not bool(state.get("stock_monitoring_enabled", True))
        await storage.save(state)
        await show_settings_page(query.message, "main", edit=True)
        return
    if data == "monitor_toggle":
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        state["monitoring_enabled"] = not bool(state.get("monitoring_enabled", True))
        await storage.save(state)
        logger.info("Monitoring enabled=%s", state["monitoring_enabled"])
        await show_settings_page(query.message, "main", edit=True)
        return
    if data.startswith("setthr:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = float(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        state["threshold"] = val
        await storage.save(state)
        await show_settings_page(query.message, "price", edit=True)
        return
    if data.startswith("setint:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        val = max(1, val)
        state["check_interval"] = val * 60
        await storage.save(state)
        await show_settings_page(query.message, "interval", edit=True)
        return
    if data.startswith("sethour:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if not (0 <= val <= 23):
            return
        state["report_hour_local"] = val
        await storage.save(state)
        await show_settings_page(query.message, "report", edit=True)
        return
    if data.startswith("setdays:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if not (1 <= val <= 7):
            return
        state["orders_days"] = val
        await storage.save(state)
        await show_settings_page(query.message, "orders", edit=True)
        return
    if data.startswith("setstock:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if not (0 <= val <= 100):
            return
        state["stock_threshold"] = val
        await storage.save(state)
        await show_settings_page(query.message, "stock", edit=True)
        return
    if data.startswith("setwarn:"):
        state = await load_state()
        if query.from_user.id not in state.get("admins", []):
            return
        try:
            val = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if not (1 <= val <= 168):
            return
        state["delivery_warn_hours"] = val
        await storage.save(state)
        await show_settings_page(query.message, "storage", edit=True)
        return
    if data in ("sync_now", "sync"):
        msg = await query.message.reply_text("⏳ Синхронизирую…")
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=msg)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_sync(fake_update, fake_context)
        return
    if data in ("check_now", "check"):
        msg = await query.message.reply_text("⏳ Проверяю…")
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=msg)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_check(fake_update, fake_context)
        return
    if data == "stock_now":
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=query.message)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_stock(fake_update, fake_context)
        return
    if data == "returns_now":
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=query.message)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_returns(fake_update, fake_context)
        return
    if data == "help_now":
        await show_main_menu(query.message, edit=True)
        return
    if data == "settings":
        await show_settings_page(query.message, "main", edit=True)
        return
    if data == "prices_now":
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=query.message)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_prices(fake_update, fake_context)
        return
    if data == "reviews_now":
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=query.message)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_reviews(fake_update, fake_context)
        return
    if data == "orders_now":
        fake_update = SimpleNamespace(effective_chat=query.message.chat, message=query.message)
        fake_context = SimpleNamespace(args=[], application=context.application)
        await cmd_orders(fake_update, fake_context)
        return
    if data == "report_now":
        await query.message.reply_text("📊 Формирую отчёт…")
        try:
            await send_daily_report(context.application)
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка отчёта: {e}")
        return

async def show_main_menu(message, edit: bool = False) -> None:
    text = "Главное меню:"
    kb = inline_main_kb()
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=kb)


def monitoring_enabled_now() -> bool:
    try:
        return bool(storage._read_sync().get("monitoring_enabled", True))
    except Exception:
        return True


def inline_main_kb() -> InlineKeyboardMarkup:
    monitor_mark = "🟢" if monitoring_enabled_now() else "🔴"
    monitor_text = "Мониторинг: вкл." if monitoring_enabled_now() else "Мониторинг: выкл."
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync_now"), InlineKeyboardButton("✅ Проверить", callback_data="check_now")],
            [InlineKeyboardButton("📋 Товары", callback_data="listpage:0"), InlineKeyboardButton("📦 Остатки", callback_data="stock_now"), InlineKeyboardButton("↩️ Возвраты", callback_data="returns_now")],
            [InlineKeyboardButton("🚚 Доставки", callback_data="ship_menu"), InlineKeyboardButton("🛒 Заказы", callback_data="orders_now"), InlineKeyboardButton("💰 Цены", callback_data="prices_now")],
            [InlineKeyboardButton("⭐ Отзывы", callback_data="reviews_now"), InlineKeyboardButton("📊 Отчёт", callback_data="report_now"), InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("🏠 Меню", callback_data="help_now"), InlineKeyboardButton(f"{monitor_mark} {monitor_text}", callback_data="settings")],
        ]
    )

async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    try:
        prices = await ozon.fetch_prices()
    except OzonError as e:
        await update.message.reply_text(f"❌ Ошибка Ozon: {e}")
        return
    if not prices:
        await update.message.reply_text("Цен не получено. Выполните /sync.")
        return
    state = await load_state()
    tracked = state.get("tracked") or {}
    lines = ["💰 Цены Ozon:\n"]
    for item in prices:
        pid = item.get("product_id")
        meta = tracked.get(str(pid), {}) if pid is not None else {}
        name = meta.get("name") or f"Товар {pid}"
        raw = item.get("raw", {}) if isinstance(item.get("raw"), dict) else item
        po = raw.get("price", {})
        if not isinstance(po, dict):
            po = {}
        idx = raw.get("price_indexes", {}) if isinstance(raw.get("price_indexes"), dict) else {}
        oz_idx = idx.get("ozon_index_data", {}) if isinstance(idx.get("ozon_index_data"), dict) else {}
        ext_idx = idx.get("external_index_data", {}) if isinstance(idx.get("external_index_data"), dict) else {}
        actions = raw.get("marketing_actions", {}) if isinstance(raw.get("marketing_actions"), dict) else {}
        act_titles = [a.get("title") for a in actions.get("actions", []) if isinstance(a, dict) and a.get("title")]
        lines.append(f"• {name} ({item.get('offer_id')})")
        lines.append(f"  С Ozon-акциями: {fmt_money(po.get('marketing_seller_price'))} ₽")
        lines.append(f"  Ozon-индекс: {fmt_money(oz_idx.get('min_price'))} ₽")
        lines.append(f"  Конкуренты: {fmt_money(ext_idx.get('min_price'))} ₽")
        lines.append("")
    state = await load_state()
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_offers = await ym_client.fetch_offers()
            if ym_offers:
                lines.append("")
                lines.append("🟡 Цены Яндекс Маркет:")
                for offer in ym_offers[:20]:
                    lines.append(
                        f"• {str(offer.get('name') or offer.get('offer_id'))[:60]} ({offer.get('offer_id')})"
                    )
                    lines.append(f"  Цена Яндекс Маркета: {fmt_money(offer.get('basic_price'))} ₽")
                    lines.append(f"  Статус: {offer.get('status') or '—'}")
                    if offer.get("stock") is not None:
                        lines.append(f"  Остаток FIT: {offer.get('stock')}")
                    lines.append("")
        except Exception as e:
            lines.append(f"YM prices unavailable: {e}")

    await update.message.reply_text("\n".join(lines).strip(), reply_markup=inline_main_kb())

async def cmd_orders(update, context):
    if not await _admin_guard(update):
        return
    state = await load_state()
    default_days = int(state.get('orders_days', 3) or 3)
    try:
        days = int(context.args[0]) if context.args else default_days
    except (ValueError, IndexError):
        days = default_days
    days = max(1, min(days, 90))
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    to = now_iso()
    try:
        posts = await ozon.fetch_postings_fbs(since, to, 50) + await ozon.fetch_postings_fbo(since, to, 50)
    except OzonError as e:
        await update.message.reply_text('Ошибка Ozon: ' + str(e), reply_markup=inline_main_kb())
        return
        await update.message.reply_text('Ошибка Ozon: ' + str(e), reply_markup=inline_main_kb())
        return

    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())
    if not posts:
        await update.message.reply_text('Заказов за ' + str(days) + ' дней не найдено.', reply_markup=inline_main_kb())
        return
    posts = sorted(posts, key=lambda p: p.get('created_at') or p.get('in_process_at') or '', reverse=True)
    lines = ['Заказы Ozon за ' + str(days) + ' дней (' + str(len(posts)) + '):']
    for p in posts[:20]:
        pn = p.get('posting_number')
        scheme = (p.get('delivery_scheme') or '').upper() or ('FBO' if p.get('storage_type') is not None or not p.get('delivery_method') else 'FBS')
        status_raw = p.get('status') or p.get('substatus') or 'n/a'
        status = ORDER_STATUS_LABELS.get(str(status_raw).lower(), str(status_raw))
        created_raw = p.get('created_at') or p.get('in_process_at') or 'n/a'
        created = fmt_date(created_raw)
        products = p.get('products', []) or []
        names = ', '.join(str(x.get('name') or x.get('offer_id')) for x in products if isinstance(x, dict))[:80]
        quantity = None
        if products and isinstance(products[0], dict):
            quantity = products[0].get('quantity')
        price = None
        if products and isinstance(products[0], dict):
            pr = products[0].get('price', {})
            if isinstance(pr, dict):
                price = pr.get('amount')
        lines.append('')
        lines.append('Заказ ' + str(pn) + ' (' + scheme + ')')
        lines.append('Статус: ' + str(status))
        lines.append('Товар: ' + names)
        qty_str = str(quantity) if quantity not in (None, '') else '1'
        price_str = str(price) if price not in (None, '') else '-'
        lines.append('Кол-во/цена: ' + qty_str + ' x ' + price_str + ' RUB')
        lines.append('Создан: ' + str(created))
    state = await load_state()
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_orders = await ym_client.fetch_orders(limit=20)
        except Exception as e:
            ym_orders = []
            lines.append(f"YM orders unavailable: {e}")
        if ym_orders:
            lines.append("")
            lines.append(f"🟡 Заказы Яндекс Маркет ({len(ym_orders)}):")
            for order in ym_orders:
                oid = order.get("id") or order.get("orderId") or order.get("order_id") or "—"
                lines.append(f"• ЯМ {oid} — {order.get('status') or order.get('substatus') or '—'}")
                created = order.get("creationDate") or order.get("createdAt") or order.get("created")
                if created:
                    lines.append(f"  Создан: {fmt_date(created)}")

    await update.message.reply_text(chr(10).join(lines), reply_markup=inline_main_kb())
    try:
        state = await load_state()
        state['orders_days'] = days
        await storage.save(state)
    except Exception:
        pass
async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    cur_min = int(state.get("check_interval", CHECK_INTERVAL_SEC)) // 60
    if not context.args:
        await update.message.reply_text(
            f"Текущий интервал проверки: {cur_min} мин.\n"
            "Использование: /setinterval 15   (в минутах, минимум 1)",
            reply_markup=inline_main_kb(),
        )
        return
    try:
        minutes = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Укажите число минут, например /setinterval 15")
        return
    if minutes < 1:
        await update.message.reply_text("Интервал не может быть меньше 1 минуты.")
        return
    state["check_interval"] = minutes * 60
    await storage.save(state)
    await update.message.reply_text(
        f"✅ Интервал проверки установлен: {minutes} мин. (вступит после текущего цикла)",
        reply_markup=inline_main_kb(),
    )

async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    try:
        prices = await ozon.fetch_prices()
    except OzonError as e:
        await update.message.reply_text(f"❌ Ошибка Ozon: {e}")
        return
    if not prices:
        await update.message.reply_text("Данных цен нет. Выполните /sync.", reply_markup=inline_main_kb())
        return
    lines = ["📉 Ценовые индексы Ozon:"]
    for item in prices:
        raw = item.get("raw", {}) if isinstance(item.get("raw"), dict) else item
        po = raw.get("price", {}) if isinstance(raw.get("price"), dict) else {}
        idx = raw.get("price_indexes", {}) if isinstance(raw.get("price_indexes"), dict) else {}
        oz = idx.get("ozon_index_data", {}) if isinstance(idx.get("ozon_index_data"), dict) else {}
        ext = idx.get("external_index_data", {}) if isinstance(idx.get("external_index_data"), dict) else {}
        lines.append(
            f"• {item.get('offer_id')} — база {fmt_money(po.get('price'))} ₽\n"
            f"  Ozon-индекс: {fmt_money(oz.get('min_price'))} ₽ ({oz.get('price_index_color')})\n"
            f"  Конкуренты (Ozon): {fmt_money(ext.get('min_price'))} ₽ | цвет {ext.get('price_index_color')}"
        )
    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())

async def cmd_reviews(update, context):
    if not await _admin_guard(update):
        return
    _mode = (context.args[0].lower() if context.args else 'per')
    n = 10
    if _mode.isdigit():
        n = int(_mode)
        _mode = 'last'
    n = max(1, min(n, 30))
    try:
        reviews = await ozon.fetch_reviews(limit=max(20, min(100, max(n, 20))))
        reviews.sort(key=lambda r: r.get('published_at') or '', reverse=True)
    except OzonError as e:
        await update.message.reply_text('❌ Ошибка Ozon: ' + str(e), reply_markup=inline_main_kb())
        return
    if not reviews:
        await update.message.reply_text('Отзывов Ozon не найдено.', reply_markup=inline_main_kb())
        return
    _per_product = _mode in ('per', 'latest', 'products')
    if _mode in ('all', 'last', 'recent'):
        _per_product = False
    if _per_product:
        _seen = set()
        selected = []
        for _r in reviews:
            _sku = _r.get('sku')
            if _sku in _seen:
                continue
            _seen.add(_sku)
            selected.append(_r)
    else:
        selected = reviews[:n]
    skus = [r.get('sku') for r in reviews if isinstance(r, dict) and r.get('sku') is not None]
    try:
        sku_info = await ozon.get_names_by_sku([int(x) for x in skus if x is not None])
    except Exception:
        sku_info = {}
    if _per_product:
        lines = ['⭐ Отзывы Ozon: последний по каждому товару (' + str(len(selected)) + ')']
    else:
        lines = ['⭐ Отзывы Ozon: показано ' + str(len(selected)) + ' из ' + str(len(reviews))]
    for r in selected:
        sku = r.get('sku')
        sku_i = int(sku) if isinstance(sku, (int, str)) and str(sku).isdigit() else None
        info = (sku_info.get(sku_i) if sku_i is not None else None) or {}
        name = info.get('name') or ('SKU ' + str(sku))
        rating = r.get('rating')
        text = str(r.get('text') or '').replace(chr(10), ' ')[:180]
        published = r.get('published_at')
        comments = r.get('comments_amount')
        photos = r.get('photos_amount')
        lines.append('')
        lines.append('⭐ Рейтинг: ' + str(rating))
        lines.append('• ' + name)
        lines.append('  Текст: ' + (text or '—'))
        lines.append('  Опубликовано: ' + str(fmt_date(published)))
        lines.append('  Комментарии: ' + str(comments) + ', фото: ' + str(photos))
    state = await load_state()
    if state.get("store_mon_ym", True) and ym_client.configured():
        try:
            ym_reviews = await ym_client.fetch_reviews(limit=n)
        except Exception as e:
            ym_reviews = []
            lines.append(f"YM reviews unavailable: {e}")
        if ym_reviews:
            lines.append("")
            lines.append('🟡 Отзывы Яндекс Маркет (' + str(len(ym_reviews)) + '):')
            for item in ym_reviews[:n]:
                name = item.get("offerId") or item.get("offer_id")
                rating = item.get("grade") if "grade" in item else item.get("rating")
                text = str(item.get("text") or "").replace(chr(10), " ")[:180]
                published = item.get("creationDate") or item.get("createDate") or item.get("published_at")
                lines.append('• ' + (str(name) if name is not None else ('SKU ' + str(item.get('sku')))) + ' ⭐' + str(rating))
                lines.append('  ' + (text or '—'))
                lines.append('  Опубликовано: ' + str(fmt_date(published)))
    await update.message.reply_text(chr(10).join(lines), reply_markup=inline_main_kb())

async def cmd_setreporttime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    if not context.args:
        await update.message.reply_text(
            f"Текущее время ежедневного отчёта: {state.get('report_hour_local', 10)}:00 (серверное время).\n"
            f"Текущее серверное время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Europe/Helsinki)\n"
            "Использование: /setreporttime 10",
            reply_markup=inline_main_kb(),
        )
        return
    try:
        hour = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Укажите час, например /setreporttime 10")
        return
    if not (0 <= hour <= 23):
        await update.message.reply_text("Час должен быть от 0 до 23.")
        return
    state["report_hour_local"] = hour
    await storage.save(state)
    await update.message.reply_text(
        f"✅ Время отчёта установлено: {hour}:00 (серверное время).",
        reply_markup=inline_main_kb(),
    )

async def cmd_setordersdays(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    if not context.args:
        await update.message.reply_text(
            f"Текущее окно заказов: {state.get('orders_days', 3)} дн.\n"
            "Использование: /setordersdays 3 (от 1 до 7)",
            reply_markup=inline_main_kb(),
        )
        return
    try:
        days = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Укажите число дней, например /setordersdays 3")
        return
    if not (1 <= days <= 7):
        await update.message.reply_text("Окно заказов может быть от 1 до 7 дней.")
        return
    state["orders_days"] = days
    await storage.save(state)
    await update.message.reply_text(f"✅ Окно заказов: {days} дн.", reply_markup=inline_main_kb())


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    await update.message.reply_text("📊 Формирую отчёт…", reply_markup=inline_main_kb())
    try:
        await send_daily_report(context.application)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка отчёта: {e}", reply_markup=inline_main_kb())


async def cmd_deliveries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    args = context.args or []
    if not args:
        await show_delivery_providers(update.message, edit=False)
        return
    provider_key = args[0].lower()
    if provider_key not in ("ozon", "yandex", "cdek"):
        # Backward-compatible: /deliveries 7 means Ozon.
        provider_key = "ozon"
        days_raw = args[0]
    else:
        days_raw = args[1] if len(args) > 1 else "7"
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))
    text, kb = await build_delivery_report(provider_key, days)
    await update.message.reply_text(text, reply_markup=kb)

async def cmd_returnstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    try:
        returns_list = await ozon.fetch_returns(limit=100)
    except OzonError as e:
        await update.message.reply_text(f"❌ Ошибка Ozon: {e}", reply_markup=inline_main_kb())
        return
    counts: Dict[str, int] = {}
    for r in returns_list:
        st = ""
        vis = r.get("visual", {}) if isinstance(r.get("visual"), dict) else {}
        if isinstance(vis.get("status"), dict):
            st = vis["status"].get("display_name") or vis["status"].get("sys_name") or ""
        if not st:
            st = r.get("type") or "неизвестно"
        counts[st] = counts.get(st, 0) + 1
    lines = [f"↩️ Возвраты ({len(returns_list)}):"]
    for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"• {k}: {v}")
    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())

async def cmd_ym(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _admin_guard(update):
        return
    state = await load_state()
    if not state.get("store_mon_ym", True):
        await update.message.reply_text("Мониторинг Яндекс Маркета выключен в Настройках.", reply_markup=inline_main_kb())
        return
    if not ym_client.configured():
        await update.message.reply_text("Яндекс Маркет не настроен. Заполните YANDEX_MARKET_API_KEY.", reply_markup=inline_main_kb())
        return
    try:
        offers = await ym_client.fetch_offers()
        orders = await ym_client.fetch_orders(limit=20)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка Yandex Market: {e}", reply_markup=inline_main_kb())
        return
    status_counts: Dict[str, int] = {}
    for offer in offers:
        st = str(offer.get("status") or "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1
    lines = [f"🟡 Яндекс Маркет — офферы ({len(offers)}):"]
    if status_counts:
        lines.append("Статусы: " + ", ".join(f"{k}: {v}" for k, v in sorted(status_counts.items())))
    if not offers:
        lines.append("Офферы не найдены.")
    for offer in offers[:20]:
        name = str(offer.get("name") or offer.get("offer_id") or "оффер")[:70]
        lines.append(f"• {name} ({offer.get('offer_id')}) — {offer.get('status') or '—'}; цена {fmt_money(offer.get('basic_price'))} ₽")
    if orders:
        lines.append("")
        lines.append(f"Заказы ЯМ ({len(orders)}):")
        for order in orders[:10]:
            oid = order.get("id") or order.get("orderId") or order.get("order_id") or "—"
            status = order.get("status") or order.get("substatus") or "—"
            created = order.get("creationDate") or order.get("createdAt") or order.get("created")
            lines.append(f"• {oid} — {status}" + (f", создан: {fmt_date(created)}" if created else ""))
    await update.message.reply_text("\n".join(lines), reply_markup=inline_main_kb())

def main() -> None:
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        print("Заполните TELEGRAM_BOT_TOKEN в .env и перезапустите.")
        return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("stocks", cmd_stock))
    app.add_handler(CommandHandler("returns", cmd_returns))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("price", cmd_prices))
    app.add_handler(CommandHandler("index", cmd_index))
    app.add_handler(CommandHandler("indexes", cmd_index))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("setreporttime", cmd_setreporttime))
    app.add_handler(CommandHandler("setordersdays", cmd_setordersdays))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("deliveries", cmd_deliveries))
    app.add_handler(CommandHandler("delivery", cmd_deliveries))
    app.add_handler(CommandHandler("shipping", cmd_deliveries))
    app.add_handler(CommandHandler("ym", cmd_ym))
    app.add_handler(CommandHandler("yandexmarket", cmd_ym))
    app.add_handler(CommandHandler("returnstats", cmd_returnstats))
    app.add_handler(CommandHandler("order", cmd_orders))
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(CommandHandler("review", cmd_reviews))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("setstockthreshold", cmd_setstockthreshold))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_menu))
    logger.info("Starting bot; data dir=%s", DATA_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
