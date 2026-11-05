"""Текстовые блоки «Тильда» для меню, отчётов и алертов.

Здесь только форматирование: никаких сетевых вызовов и записи в Тильду.
Блоки для /prices, /list, /orders и /tilda — обычный текст (без HTML),
а ежедневный отчёт и алерты уходят с parse_mode=HTML и экранируются.
"""

from __future__ import annotations

from datetime import datetime
from html import escape as html_escape
from typing import Iterable, List, Optional

from tilda import TildaOrder, TildaProduct

TILDA_EMOJI = "🟣"
TILDA_TITLE = "Тильда"
TILDA_TITLE_GEN = "Тильды"


def money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(number - round(number)) < 0.005:
        return f"{int(round(number)):,}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ").replace(".", ",")


def currency_sign(currency: str) -> str:
    return "₽" if (currency or "").strip().upper() in ("RUB", "RUR", "₽") else (currency or "")


def short(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M")


def plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(int(number)) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def products_line(product: TildaProduct) -> str:
    sku = f" ({product.sku})" if product.sku else ""
    stock = "" if product.quantity is None else f", остаток {product.quantity}"
    return f"• {short(product.title, 70)}{sku} — {money(product.price)} ₽{stock}"


def format_products_block(products: Iterable[TildaProduct]) -> List[str]:
    items = list(products)
    if not items:
        return []
    lines = ["", f"{TILDA_EMOJI} {TILDA_TITLE} ({len(items)}):"]
    for product in items:
        lines.append(products_line(product))
    return lines


def format_prices_block(products: Iterable[TildaProduct]) -> List[str]:
    items = list(products)
    if not items:
        return []
    lines = ["", f"{TILDA_EMOJI} Цены {TILDA_TITLE_GEN} ({len(items)}):"]
    for product in items:
        sku = f" ({product.sku})" if product.sku else ""
        note = ""
        if product.price_old and product.price_old > product.price:
            note = f" (было {money(product.price_old)} ₽)"
        stock = "" if product.quantity is None else f", остаток {product.quantity}"
        lines.append(f"• {short(product.title, 70)}{sku} — {money(product.price)} ₽{note}{stock}")
    return lines


def format_orders_block(orders: Iterable[TildaOrder], days: int, limit: int = 10) -> List[str]:
    items = list(orders)
    lines = ["", f"{TILDA_EMOJI} Заказы {TILDA_TITLE_GEN} за {days} дн ({len(items)}):"]
    if not items:
        lines.append("Заказов нет.")
        return lines
    for order in items[:limit]:
        paid = "оплачен" if order.paid else "не оплачен"
        number = order.number or "—"
        amount = f"{money(order.amount)} {currency_sign(order.currency)}"
        lines.append(f"• № {number} — {order.status}, {amount}, {paid}")
        contact = ", ".join(part for part in (order.customer, order.city) if part)
        if contact:
            lines.append(f"  {short(contact, 90)}")
        if order.items:
            qty = f" × {order.quantity}" if order.quantity else ""
            lines.append(f"  {short(order.items[0], 70)}{qty}")
        if order.delivery:
            lines.append(f"  Доставка: {short(order.delivery, 80)}")
        lines.append(f"  Создан: {fmt_dt(order.created)}")
    if len(items) > limit:
        lines.append(f"… и ещё {len(items) - limit} " + plural(len(items) - limit, "заказ", "заказа", "заказов"))
    return lines


def format_daily_block(orders: Iterable[TildaOrder], since: datetime) -> List[str]:
    items = [o for o in orders if o.created is not None and o.created >= since]
    if not items:
        return ["", f"{TILDA_EMOJI} {TILDA_TITLE}: заказов за 24ч нет."]
    paid = [o for o in items if o.paid]
    revenue = sum(o.amount for o in paid)
    lines = [
        "",
        f"{TILDA_EMOJI} {TILDA_TITLE}: заказов за 24ч {len(items)}, оплачено {len(paid)}, выручка {money(revenue)} ₽",
    ]
    for order in items[:8]:
        number = html_escape(order.number or "—")
        lines.append(f"• № {number} — {html_escape(order.status)}, {money(order.amount)} {html_escape(currency_sign(order.currency))}")
        if order.items:
            qty = f" × {order.quantity}" if order.quantity else ""
            lines.append(f"  {html_escape(short(order.items[0], 60))}{qty}")
    if len(items) > 8:
        lines.append(f"… и ещё {len(items) - 8} " + plural(len(items) - 8, "заказ", "заказа", "заказов"))
    return lines


def format_new_paid_alert(order: TildaOrder) -> str:
    number = html_escape(order.number or "—")
    lines = [
        f"{TILDA_EMOJI} <b>Новый оплаченный заказ на сайте</b>",
        f"№ {number} — {money(order.amount)} {html_escape(currency_sign(order.currency))}",
    ]
    contact = ", ".join(part for part in (order.customer, order.city) if part)
    if contact:
        lines.append(f"Клиент: {html_escape(short(contact, 100))}")
    if order.phone:
        lines.append(f"Телефон: {html_escape(order.phone)}")
    if order.items:
        qty = f" × {order.quantity}" if order.quantity else ""
        lines.append(f"Товар: {html_escape(short(order.items[0], 90))}{qty}")
    if order.delivery:
        lines.append(f"Доставка: {html_escape(short(order.delivery, 90))}")
    if order.created:
        lines.append(f"Создан: {fmt_dt(order.created)}")
    lines.append(f"Статус: {html_escape(order.status)}")
    return "\n".join(lines)
