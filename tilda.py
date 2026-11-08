"""Клиент Tilda Store через MCP.

Чтение: товары, заказы, статусы. Запись (только по явному подтверждению
владельца в чате): смена статуса заказа и публичный комментарий, который
Тильда отправляет покупателю письмом при изменении.

Запрос идёт напрямую по HTTP (Streamable MCP), без дочернего процесса, чтобы на
сервере не плодить лишние ``python`` и не зависеть от прав доступа к файлам.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))
DEFAULT_MCP_URL = "https://tilda.ru/api/mcp/"
# Версия протокола, которую принимает MCP Тильды (2025-03-26 отклоняется).
PROTOCOL_VERSION = "2025-11-25"
PAGE_SIZE = 500


class TildaError(Exception):
    """Ошибка интеграции с Тильдой: сеть, протокол или неверный ответ."""


def parse_tilda_dt(value: Any) -> Optional[datetime]:
    """Разбирает московское (UTC+3) время Тильды, например ``2026-11-03 13:20:56``."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK_TZ)
    return dt


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _decode_json(payload: Any) -> Any:
    """Тильда кладёт JSON в текстовое поле ответа MCP."""
    if isinstance(payload, (dict, list)):
        return payload
    text = _text(payload)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


@dataclass
class TildaProduct:
    uid: str
    title: str
    sku: str
    price: float
    price_old: float
    quantity: Optional[int]
    off: bool = False


@dataclass
class TildaOrder:
    number: str
    serial: Optional[int]
    created: Optional[datetime]
    paid_at: Optional[datetime]
    status: str
    paid: bool
    amount: float
    currency: str
    customer: str
    phone: str
    city: str
    delivery: str
    items: List[str] = field(default_factory=list)
    quantity: int = 0
    email: str = ""
    status_id: str = ""
    tracking_type: str = ""
    tracking_number: str = ""
    public_comment: str = ""

    @property
    def is_cancelled(self) -> bool:
        return self.status.strip().lower().startswith("отмен")


SYSTEM_STATUS_TITLES = {
    "2": "Новый",
    "3": "Выполнен",
    "4": "Отменён",
}


class TildaClient:
    """Минимальный клиент Tilda Store (только чтение)."""

    def __init__(self, project_id: Any, token: str, mcp_url: Optional[str] = None):
        pid = _int_or_none(project_id)
        self.project_id = pid or 0
        self.token = (token or "").strip()
        self.mcp_url = (mcp_url or DEFAULT_MCP_URL).strip()
        if not self.mcp_url.endswith("/"):
            self.mcp_url += "/"
        self._session_id: Optional[str] = None
        self._request_id = 0
        self._status_cache: Dict[str, str] = {}

    def configured(self) -> bool:
        return bool(self.project_id and self.token)

    # ---------- транспорт ----------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _split_response(response: httpx.Response) -> List[Dict[str, Any]]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages: List[Dict[str, Any]] = []
            for block in response.text.split("\n\n"):
                data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
                if not data:
                    continue
                try:
                    messages.append(json.loads("\n".join(data)))
                except ValueError:
                    continue
            return messages
        try:
            return [response.json()]
        except ValueError as e:
            raise TildaError("Тильда вернула не JSON: " + response.text[:200]) from e

    async def _post(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> httpx.Response:
        try:
            return await client.post(self.mcp_url, headers=self._headers(), json=payload)
        except httpx.HTTPError as e:
            raise TildaError("Нет связи с Тильдой: " + str(e)) from e

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ozon-goods-bot", "version": "1.0"},
            },
        }
        response = await self._post(client, payload)
        if response.status_code >= 400:
            raise TildaError("Тильда отклонила подключение: " + response.text[:300])
        self._session_id = response.headers.get("mcp-session-id") or self._session_id
        await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _call(self, client: httpx.AsyncClient, name: str, arguments: Dict[str, Any]) -> Any:
        if self._session_id is None:
            await self._initialize(client)
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await self._post(client, payload)
        if response.status_code >= 400:
            raise TildaError("Тильда вернула HTTP " + str(response.status_code) + ": " + response.text[:300])
        messages = self._split_response(response)
        target = None
        for message in messages:
            if message.get("id") == self._request_id:
                target = message
                break
        if target is None:
            target = messages[-1] if messages else {}
        error = target.get("error")
        if isinstance(error, dict):
            raise TildaError("Тильда: " + _text(error.get("message") or error))
        result = target.get("result") or {}
        if result.get("isError"):
            texts = [_text(part.get("text")) for part in (result.get("content") or []) if isinstance(part, dict)]
            raise TildaError("Тильда: " + (" ".join(t for t in texts if t) or "неизвестная ошибка"))
        texts = []
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(_text(part.get("text")))
        return _decode_json("\n".join(texts))

    async def _with_client(self, handler) -> Any:
        if not self.configured():
            raise TildaError("Тильда не настроена: нет TILDA_PROJECT_ID или TILDA_MCP_TOKEN")
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            try:
                return await handler(client)
            finally:
                self._session_id = None

    # ---------- данные ----------

    async def fetch_products(self) -> List[TildaProduct]:
        async def handler(client: httpx.AsyncClient) -> List[TildaProduct]:
            products: List[TildaProduct] = []
            for slice_no in range(1, 11):
                data = await self._call(
                    client,
                    "store_get_products_list",
                    {
                        "projectid": self.project_id,
                        "slice": slice_no,
                        "slicesize": PAGE_SIZE,
                        "filter": {"off": ""},
                    },
                )
                items = data.get("products") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    off = _text(item.get("off")).lower()
                    if off == "y":
                        # Скрытые товары не показываем даже если фильтр их пропустил.
                        continue
                    products.append(
                        TildaProduct(
                            uid=_text(item.get("uid") or item.get("id")),
                            title=_text(item.get("title")) or "Без названия",
                            sku=_text(item.get("sku")),
                            price=_float(item.get("price")),
                            price_old=_float(item.get("priceold")),
                            quantity=_int_or_none(item.get("quantity")),
                            off=off == "y",
                        )
                    )
                if len(items) < PAGE_SIZE:
                    break
            return products

        return await self._with_client(handler)

    # ---------- запись (только по подтверждению владельца) ----------

    async def fetch_order_detail(self, order_number: str) -> Optional[TildaOrder]:
        """Полные данные одного заказа (public_comment, tracking — есть только здесь)."""
        async def handler(client: httpx.AsyncClient) -> Optional[TildaOrder]:
            titles = await self.fetch_status_titles(client)
            data = await self._call(
                client,
                "store_get_orders",
                {"projectid": self.project_id, "formsref": int(order_number)},
            )
            item = data.get("order") if isinstance(data, dict) and isinstance(data.get("order"), dict) else data
            if not isinstance(item, dict) or not item:
                return None
            return self._order_from_raw(item, titles)

        return await self._with_client(handler)

    async def set_order_status(self, order_number: str, status_id: int) -> None:
        """Меняет статус заказа (например, 1001 «Ожидает в ПВЗ», 3 «Завершён»)."""
        async def handler(client: httpx.AsyncClient) -> None:
            await self._call(
                client,
                "store_set_orders_status",
                {
                    "projectid": self.project_id,
                    "formsref": int(order_number),
                    "orderstatusid": int(status_id),
                },
            )

        await self._with_client(handler)

    async def set_public_comment(self, order_number: str, text: str) -> None:
        """Обновляет публичный комментарий заказа (отправляется покупателю письмом)."""
        async def handler(client: httpx.AsyncClient) -> None:
            await self._call(
                client,
                "store_save_orders_field",
                {
                    "projectid": self.project_id,
                    "formsref": int(order_number),
                    "public_comment": text,
                },
            )

        await self._with_client(handler)

    async def fetch_status_titles(self, client: Optional[httpx.AsyncClient] = None) -> Dict[str, str]:
        if self._status_cache:
            return dict(self._status_cache)
        if client is None:
            async def handler(c: httpx.AsyncClient) -> Dict[str, str]:
                return await self.fetch_status_titles(c)

            return await self._with_client(handler)
        data = await self._call(client, "store_get_orders_statuses", {"projectid": self.project_id})
        titles: Dict[str, str] = {}
        if isinstance(data, list):
            for status in data:
                if not isinstance(status, dict):
                    continue
                sid = _text(status.get("id"))
                if not sid:
                    continue
                title = _text(status.get("title")) or _text(status.get("name")) or SYSTEM_STATUS_TITLES.get(sid, "")
                if title:
                    titles[sid] = title
        for sid, title in SYSTEM_STATUS_TITLES.items():
            titles.setdefault(sid, title)
        self._status_cache = titles
        return dict(titles)

    @staticmethod
    def _order_from_raw(item: Dict[str, Any], titles: Dict[str, str]) -> TildaOrder:
        shipping = item.get("shipping") if isinstance(item.get("shipping"), dict) else {}
        status_id = _text(item.get("statusid"))
        status = titles.get(status_id) or _text(item.get("status")) or "Неизвестный статус"
        products = item.get("products") if isinstance(item.get("products"), list) else []
        items: List[str] = []
        quantity = 0
        for product in products:
            if not isinstance(product, dict):
                continue
            title = _text(product.get("title"))
            if title:
                items.append(title)
            quantity += _int_or_none(product.get("quantity")) or 1
        if not quantity:
            quantity = _int_or_none(item.get("quantity")) or 0
        return TildaOrder(
            number=_text(item.get("formsref") or item.get("orderid")),
            serial=_int_or_none(item.get("serialid")),
            created=parse_tilda_dt(item.get("created")),
            paid_at=parse_tilda_dt(item.get("payed")),
            status=status,
            paid=_int_or_none(item.get("paystatusid")) == 1,
            amount=_float(item.get("amount_total") or item.get("amount")),
            currency=_text(item.get("currency")) or "RUB",
            customer=_text(item.get("name")),
            phone=_text(item.get("phone")),
            city=_text(shipping.get("city")),
            delivery=_text(shipping.get("name")) or _text(shipping.get("pickup-name")),
            items=items,
            quantity=quantity,
            email=_text(item.get("email")),
            status_id=status_id,
            tracking_type=_text((item.get("tracking") or {}).get("type")) if isinstance(item.get("tracking"), dict) else "",
            tracking_number=_text((item.get("tracking") or {}).get("number")) if isinstance(item.get("tracking"), dict) else "",
            public_comment=_text(item.get("public_comment")),
        )

    async def fetch_orders(self, days: int = 15) -> List[TildaOrder]:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))

        async def handler(client: httpx.AsyncClient) -> List[TildaOrder]:
            titles = await self.fetch_status_titles(client)
            orders: List[TildaOrder] = []
            seen: set = set()
            for slice_no in range(1, 21):
                data = await self._call(
                    client,
                    "store_get_orders_list",
                    {"projectid": self.project_id, "slice": slice_no, "slicesize": PAGE_SIZE},
                )
                items = data.get("orders") if isinstance(data, dict) else data
                if not isinstance(items, list) or not items:
                    break
                fresh = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    order = self._order_from_raw(item, titles)
                    key = order.number or str(order.serial or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    if order.is_cancelled:
                        # Отменённые заказы сайта в отчётах не показываем.
                        continue
                    if order.created is None or order.created >= since:
                        orders.append(order)
                if fresh == 0 or len(items) < PAGE_SIZE:
                    break
            orders.sort(key=lambda o: o.created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return orders

        return await self._with_client(handler)
