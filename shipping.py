"""Delivery providers: Ozon, Yandex Delivery and CDEK.

The module keeps provider-specific API details behind a small common interface so
new carriers can be added without touching the bot menus and report formatting.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


class ShippingError(Exception):
    pass


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO dates from Ozon/Yandex/CDEK, including CDEK's +0000 form."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # CDEK v2 returns +0000, which old Python versions dislike.
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Shipment:
    provider: str
    shipment_id: str
    status: str
    created_at: Optional[str] = None
    expected_at: Optional[str] = None
    delivered_at: Optional[str] = None
    pickup_ready_at: Optional[str] = None
    storage_expires_at: Optional[str] = None
    product: Optional[str] = None
    quantity: Optional[int] = None
    destination: Optional[str] = None
    pickup_point: Optional[str] = None
    tracking: Optional[str] = None
    status_code: Optional[str] = None
    is_delivered: bool = False
    is_cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.shipment_id}"

    @property
    def is_transit(self) -> bool:
        return not self.is_delivered and not self.is_cancelled

    @property
    def is_pickup_ready(self) -> bool:
        return bool(self.pickup_ready_at) and not self.is_delivered and not self.is_cancelled


class DeliveryProvider:
    key = ""
    title = ""
    emoji = "🚚"

    def configured(self) -> bool:
        return False

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Ozon
# ---------------------------------------------------------------------------

OZON_STATUS_LABELS = {
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_approve": "Ожидает подтверждения",
    "awaiting_deliver": "Ожидает отправки",
    "delivering": "Доставляется",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
    "posting_created": "Ожидает сборки",
    "posting_on_way_to_city": "В пути в город получателя",
    "posting_received": "Доставлен",
    "posting_canceled": "Отменён",
}


class OzonDeliveryProvider(DeliveryProvider):
    key = "ozon"
    title = "Ozon"
    emoji = "🟣"

    def __init__(self, ozon_client: Any):
        self.ozon = ozon_client

    def configured(self) -> bool:
        return bool(self.ozon and getattr(self.ozon, "configured", lambda: False)())

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        if not self.configured():
            raise ShippingError("Ozon API не настроен")
        try:
            fbs = await self.ozon.fetch_postings_fbs(since.isoformat(), to.isoformat(), min(limit, 100))
            fbo = await self.ozon.fetch_postings_fbo(since.isoformat(), to.isoformat(), min(limit, 100))
        except Exception as e:
            raise ShippingError(f"Ozon: {e}") from e

        result: List[Shipment] = []
        ozon_rows = [(posting, "FBS") for posting in (fbs or []) if isinstance(posting, dict)]
        ozon_rows += [(posting, "FBO") for posting in (fbo or []) if isinstance(posting, dict)]
        for posting, scheme in ozon_rows:
            if not isinstance(posting, dict):
                continue
            status_raw = str(posting.get("status") or "").lower()
            substatus = str(posting.get("substatus") or "").lower()
            status = OZON_STATUS_LABELS.get(status_raw) or OZON_STATUS_LABELS.get(substatus) or status_raw or "—"
            posted_scheme = str(posting.get("delivery_schema") or "").upper()
            if posted_scheme in ("FBS", "FBO"):
                scheme = posted_scheme
            analytics = posting.get("analytics_data") if isinstance(posting.get("analytics_data"), dict) else {}
            products = posting.get("products") if isinstance(posting.get("products"), list) else []
            first = products[0] if products and isinstance(products[0], dict) else {}
            qty = 0
            for p in products:
                if isinstance(p, dict):
                    try:
                        qty += int(p.get("quantity") or 1)
                    except (TypeError, ValueError):
                        qty += 1
            name = first.get("name") or first.get("offer_id")
            delivered = status_raw in ("delivered", "posting_received") or substatus in ("posting_received",)
            cancelled = "cancel" in status_raw or "cancel" in substatus
            # Ozon can expose a PVZ-ready status in some delivery schemes. Keep
            # this generic; unknown statuses simply won't trigger pickup alerts.
            pickup_ready = (
                bool(analytics.get("delivery_type") == "PVZ")
                and any(word in (status_raw + " " + substatus) for word in ("pickup", "ready_for_pickup", "pvz"))
            )
            expected = analytics.get("delivery_date_begin") or analytics.get("delivery_date_end") or posting.get("delivering_date")
            delivered_at = None
            if delivered:
                delivered_at = posting.get("delivering_date") or analytics.get("delivery_date_end") or analytics.get("delivery_date_begin")
            destination = analytics.get("city") or posting.get("destination_place_name") or analytics.get("region")
            if not destination:
                fin = posting.get("financial_data") if isinstance(posting.get("financial_data"), dict) else {}
                destination = fin.get("cluster_to")
            result.append(
                Shipment(
                    provider=self.key,
                    shipment_id=str(posting.get("posting_number") or posting.get("order_number") or "—"),
                    status=str(status),
                    created_at=posting.get("in_process_at") or posting.get("created_at"),
                    expected_at=expected,
                    delivered_at=delivered_at,
                    pickup_ready_at=posting.get("delivering_date") if pickup_ready else None,
                    product=str(name) if name else None,
                    quantity=qty or None,
                    destination=str(destination) if destination else None,
                    pickup_point=str(destination) if pickup_ready and destination else None,
                    tracking=str(posting.get("tracking_number") or posting.get("posting_number") or ""),
                    status_code=substatus or status_raw,
                    is_delivered=delivered,
                    is_cancelled=cancelled,
                    extra={"scheme": scheme, "raw_status": status_raw, "substatus": substatus},
                )
            )
        return result[:limit]


# ---------------------------------------------------------------------------
# Yandex Delivery (express API)
# ---------------------------------------------------------------------------

YANDEX_STATUS_LABELS = {
    "new": "Новый",
    "estimating": "Оценивается",
    "estimating_failed": "Ошибка оценки",
    "ready_for_approval": "Готов к подтверждению",
    "accepted": "Принят",
    "performer_lookup": "Ищет курьера",
    "performer_draft": "Подбирает курьера",
    "performer_found": "Курьер назначен",
    "performer_not_found": "Курьер не найден",
    "pickup_arrived": "Курьер в точке забора",
    "ready_for_pickup_confirmation": "Ожидает подтверждения забора",
    "pickuped": "Забран курьером",
    "delivery_arrived": "Курьер в точке доставки",
    "ready_for_delivery_confirmation": "Ожидает подтверждения доставки",
    "delivered": "Доставлен",
    "delivered_finish": "Доставлен (завершён)",
    "returning": "Возвращается",
    "return_arrived": "Возврат прибыл",
    "ready_for_return_confirmation": "Ожидает подтверждения возврата",
    "returned": "Возвращён",
    "returned_finish": "Возвращён (завершён)",
    "failed": "Ошибка",
    "cancelled": "Отменён",
    "cancelled_with_payment": "Отменён с оплатой",
    "cancelled_by_taxi": "Отменён сервисом",
    "cancelled_with_items_on_hands": "Отменён, товар у курьера",
}


class YandexDeliveryProvider(DeliveryProvider):
    key = "yandex"
    title = "Яндекс Доставка"
    emoji = "🟡"
    BASE_URL = "https://b2b.taxi.yandex.net"

    def __init__(self, token: str):
        self.token = (token or "").strip()

    def configured(self) -> bool:
        return bool(self.token)

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        if not self.configured():
            raise ShippingError("Яндекс Доставка не настроена: заполните YANDEX_DELIVERY_TOKEN")
        payload = {
            "limit": max(1, min(int(limit), 1000)),
            "offset": 0,
            "created_from": since.isoformat(),
            "created_to": to.isoformat(),
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.BASE_URL + "/b2b/cargo/integration/v2/claims/search",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ShippingError(f"Яндекс Доставка: сетевая ошибка: {e}") from e
        if resp.status_code >= 400:
            raise ShippingError(f"Яндекс Доставка: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ShippingError("Яндекс Доставка: некорректный ответ API") from e
        claims = data.get("claims") if isinstance(data, dict) else None
        if not isinstance(claims, list):
            return []

        result: List[Shipment] = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            status_raw = str(claim.get("status") or "").lower()
            status = YANDEX_STATUS_LABELS.get(status_raw, status_raw or "—")
            delivered = status_raw in ("delivered", "delivered_finish")
            cancelled = status_raw.startswith("cancelled") or status_raw in ("failed", "returned", "returned_finish")
            items = claim.get("items") if isinstance(claim.get("items"), list) else []
            qty = 0
            names: List[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    qty += int(item.get("quantity") or 1)
                except (TypeError, ValueError):
                    qty += 1
                title = item.get("title")
                if title:
                    names.append(str(title))
            product = " / ".join(names[:2]) + ("…" if len(names) > 2 else "") if names else None

            destination = None
            destination_addr = None
            pickup_ready_at = None
            expected = claim.get("due")
            delivered_at = None
            route_points = claim.get("route_points") if isinstance(claim.get("route_points"), list) else []
            for point in route_points:
                if not isinstance(point, dict) or point.get("type") != "destination":
                    continue
                addr = point.get("address") if isinstance(point.get("address"), dict) else {}
                destination = addr.get("fullname") or addr.get("shortname") or destination
                destination_addr = destination
                interval = point.get("expected_visit_interval") if isinstance(point.get("expected_visit_interval"), dict) else {}
                expected = expected or interval.get("to") or interval.get("from")
                visited = point.get("visited_at") if isinstance(point.get("visited_at"), dict) else {}
                delivered_at = visited.get("actual") or visited.get("expected") or delivered_at
                # Yandex express is courier delivery, not a PVZ handover. Do not
                # treat "delivery_arrived" as PVZ-ready.
            if delivered and not delivered_at:
                delivered_at = claim.get("updated_ts")

            performer = claim.get("performer_info") if isinstance(claim.get("performer_info"), dict) else {}
            result.append(
                Shipment(
                    provider=self.key,
                    shipment_id=str(claim.get("id") or "—"),
                    status=str(status),
                    created_at=claim.get("created_ts"),
                    expected_at=expected,
                    delivered_at=delivered_at,
                    pickup_ready_at=pickup_ready_at,
                    product=product,
                    quantity=qty or None,
                    destination=destination_addr,
                    pickup_point=None,
                    tracking=str(claim.get("shipping_document") or claim.get("id") or ""),
                    status_code=status_raw,
                    is_delivered=delivered,
                    is_cancelled=cancelled,
                    extra={"performer": performer, "raw_status": status_raw},
                )
            )
        return result[:limit]


# ---------------------------------------------------------------------------
# CDEK
# ---------------------------------------------------------------------------

CDEK_STATUS_LABELS = {
    "CREATED": "Создан",
    "ACCEPTED": "Принят",
    "RECEIVED_AT_SHIPMENT_WAREHOUSE": "Принят на склад отправителя",
    "READY_FOR_SHIPMENT_IN_SENDER_CITY": "Готов к отправке",
    "TAKEN_BY_TRANSPORTER_FROM_SENDER_CITY": "Сдан перевозчику",
    "SENT_TO_TRANSIT_CITY": "Отправлен в транзитный город",
    "ACCEPTED_IN_TRANSIT_CITY": "Встречен в транзитном городе",
    "ACCEPTED_AT_TRANSIT_WAREHOUSE": "Принят на склад транзита",
    "READY_FOR_SHIPMENT_IN_TRANSIT_CITY": "Выдан на отправку в транзите",
    "TAKEN_BY_TRANSPORTER_FROM_TRANSIT_CITY": "Сдан перевозчику в транзите",
    "SENT_TO_RECIPIENT_CITY": "Отправлен в город получателя",
    "ACCEPTED_IN_RECIPIENT_CITY": "Встречен в городе получателя",
    "ACCEPTED_AT_PICK_UP_POINT": "Готов к выдаче в ПВЗ",
    "DELIVERED": "Доставлен",
    "NOT_DELIVERED": "Не доставлен",
    "RETURNED_TO_SENDER_WAREHOUSE": "Возвращён на склад отправителя",
    "RETURNED_TO_SENDER": "Возвращён отправителю",
}


class CdekDeliveryProvider(DeliveryProvider):
    key = "cdek"
    title = "СДЭК"
    emoji = "🟢"

    def __init__(
        self,
        account: str,
        secure_password: str,
        v1_url: str = "https://integration.cdek.ru",
        v2_url: str = "https://api.cdek.ru",
        storage_days: int = 7,
    ):
        self.account = (account or "").strip()
        self.secure_password = (secure_password or "").strip()
        self.v1_url = (v1_url or "https://integration.cdek.ru").rstrip("/") + "/"
        self.v2_url = (v2_url or "https://api.cdek.ru").rstrip("/")
        self.storage_days = max(1, int(storage_days or 7))
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._detail_sem = asyncio.Semaphore(5)

    def configured(self) -> bool:
        return bool(self.account and self.secure_password)

    async def _v2_token(self) -> str:
        now_ts = datetime.now(timezone.utc).timestamp()
        if self._token and now_ts < self._token_exp - 60:
            return self._token
        async with self._token_lock:
            now_ts = datetime.now(timezone.utc).timestamp()
            if self._token and now_ts < self._token_exp - 60:
                return self._token
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        self.v2_url + "/v2/oauth/token?parameters",
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self.account,
                            "client_secret": self.secure_password,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except httpx.HTTPError as e:
                raise ShippingError(f"СДЭК: сетевая ошибка авторизации: {e}") from e
            if resp.status_code >= 400:
                raise ShippingError(f"СДЭК: ошибка авторизации HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            token = data.get("access_token") if isinstance(data, dict) else None
            if not token:
                raise ShippingError("СДЭК: API не вернул access_token")
            self._token = str(token)
            try:
                expires = int(data.get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires = 3600
            self._token_exp = now_ts + max(60, expires)
            return self._token

    async def _v2_get_order(self, cdek_number: str) -> Optional[Dict[str, Any]]:
        token = await self._v2_token()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    self.v2_url + "/v2/orders",
                    params={"cdek_number": cdek_number},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        entity = data.get("entity") if isinstance(data, dict) else None
        return entity if isinstance(entity, dict) else None

    def _v15_request_xml(self, since: datetime, to: datetime) -> Dict[str, str]:
        auth_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        secure = hashlib.md5((auth_date + "&" + self.secure_password).encode("utf-8")).hexdigest()
        date_first = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        date_last = to.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<StatusReport Date="{auth_date}" Account="{self.account}" Secure="{secure}" '
            'ShowHistory="1" ShowReturnOrder="1" ShowReturnOrderHistory="1">'
            f'<ChangePeriod DateFirst="{date_first}" DateLast="{date_last}" '
            f'DateBeg="{date_first}" DateEnd="{date_last}"/>'
            "</StatusReport>"
        )
        return {"xml_request": xml}

    async def _fetch_v15_orders(self, since: datetime, to: datetime) -> List[ET.Element]:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    urljoin(self.v1_url, "status_report_h.php"),
                    data=self._v15_request_xml(since, to),
                )
        except httpx.HTTPError as e:
            raise ShippingError(f"СДЭК: сетевая ошибка: {e}") from e
        if resp.status_code >= 400:
            raise ShippingError(f"СДЭК: HTTP {resp.status_code}: {resp.text[:300]}")
        text = resp.content.decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise ShippingError(f"СДЭК: некорректный XML ответа: {e}") from e
        return [el for el in root.findall("Order") if isinstance(el.tag, str)]

    def _build_from_detail(
        self,
        entity: Dict[str, Any],
        fallback_number: str = "",
        fallback_dispatch: str = "",
        fallback_status: str = "",
        fallback_delivery_date: str = "",
    ) -> Shipment:
        statuses = entity.get("statuses") if isinstance(entity.get("statuses"), list) else []
        latest = statuses[0] if statuses and isinstance(statuses[0], dict) else {}
        code = str(latest.get("code") or "")
        status_name = latest.get("name") or CDEK_STATUS_LABELS.get(code) or code or fallback_status or "—"
        status_lower = str(status_name).lower()
        delivered = code == "DELIVERED" or status_lower.startswith("вручен")
        cancelled = code in ("NOT_DELIVERED", "RETURNED_TO_SENDER", "RETURNED_TO_SENDER_WAREHOUSE") or any(
            word in status_lower for word in ("отмен", "возвращ")
        )

        delivered_at = None
        pickup_ready_at = None
        created_at = None
        for st in statuses:
            if not isinstance(st, dict):
                continue
            st_code = str(st.get("code") or "")
            st_name = str(st.get("name") or "").lower()
            dt = st.get("date_time")
            if st_code == "CREATED" and dt:
                created_at = created_at or dt
            if st_code == "DELIVERED" and dt:
                delivered_at = delivered_at or dt
            if st_code == "ACCEPTED_AT_PICK_UP_POINT" or "до востребования" in st_name or "готов к выдаче" in st_name:
                pickup_ready_at = pickup_ready_at or dt
        if delivered and not delivered_at:
            delivered_at = fallback_delivery_date or entity.get("delivery_date")

        packages = entity.get("packages") if isinstance(entity.get("packages"), list) else []
        names: List[str] = []
        qty = 0
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            for item in pkg.get("items") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    qty += int(item.get("amount") or 1)
                except (TypeError, ValueError):
                    qty += 1
                name = item.get("name")
                if name and str(name) not in names:
                    names.append(str(name))
        product = " / ".join(names[:2]) + ("…" if len(names) > 2 else "") if names else None

        to_loc = entity.get("to_location") if isinstance(entity.get("to_location"), dict) else {}
        destination = to_loc.get("address") or to_loc.get("city")
        pickup_point = destination if pickup_ready_at else None
        expected = entity.get("planned_delivery_date")
        if not expected and isinstance(entity.get("delivery_detail"), dict):
            expected = entity["delivery_detail"].get("delivery_date")
        storage = entity.get("keep_free_until") if pickup_ready_at else None
        if pickup_ready_at and not storage and not delivered:
            ready_dt = parse_dt(pickup_ready_at)
            if ready_dt is not None:
                from datetime import timedelta

                storage = (ready_dt + timedelta(days=self.storage_days)).isoformat()

        cdek_recipient = entity.get("recipient") if isinstance(entity.get("recipient"), dict) else {}
        cdek_recipient_name = cdek_recipient.get("name") or cdek_recipient.get("company")
        cdek_recipient_phone = None
        cdek_recipient_phones = cdek_recipient.get("phones")
        if isinstance(cdek_recipient_phones, list) and cdek_recipient_phones:
            first_cdek_phone = cdek_recipient_phones[0]
            if isinstance(first_cdek_phone, dict):
                cdek_recipient_phone = first_cdek_phone.get("number")

        return Shipment(
            provider=self.key,
            shipment_id=str(entity.get("cdek_number") or fallback_dispatch or entity.get("number") or fallback_number or "—"),
            status=str(status_name),
            created_at=created_at,
            expected_at=expected,
            delivered_at=delivered_at,
            pickup_ready_at=pickup_ready_at,
            storage_expires_at=storage,
            product=product,
            quantity=qty or None,
            destination=str(destination) if destination else None,
            pickup_point=str(pickup_point) if pickup_point else None,
            tracking=str(entity.get("cdek_number") or fallback_dispatch or ""),
            status_code=code or None,
            is_delivered=delivered,
            is_cancelled=cancelled,
            extra={
                "im_number": entity.get("number") or fallback_number,
                "delivery_point": entity.get("delivery_point"),
                "storage_estimated": bool(pickup_ready_at and not entity.get("keep_free_until") and not delivered),
                "status_time": latest.get("date_time"),
                "recipient_name": cdek_recipient_name,
                "recipient_phone": cdek_recipient_phone,
                "recipient_email": cdek_recipient.get("email"),
            },
        )

    def _build_from_xml(
        self,
        order_el: ET.Element,
    ) -> Shipment:
        number = order_el.attrib.get("Number") or ""
        dispatch = order_el.attrib.get("DispatchNumber") or ""
        delivery_date = order_el.attrib.get("DeliveryDate") or ""
        status_el = order_el.find("Status")
        status_name = "—"
        status_code = ""
        status_date = ""
        city = ""
        if status_el is not None:
            status_name = status_el.attrib.get("Description") or "—"
            status_code = status_el.attrib.get("Code") or ""
            status_date = status_el.attrib.get("Date") or ""
            city = status_el.attrib.get("CityName") or ""
        status_lower = status_name.lower()
        delivered = status_code == "4" or status_lower.startswith("вручен") or bool(delivery_date)
        cancelled = status_code in ("5", "16", "17") or any(word in status_lower for word in ("отмен", "возвращ"))
        pickup_ready_at = status_date if ("до востребования" in status_lower or "готов к выдаче" in status_lower) else None
        created_at = None
        for state in order_el.findall("State"):
            if state.attrib.get("Code") == "1":
                created_at = state.attrib.get("Date")
                break
        storage = None
        if pickup_ready_at and not delivered:
            ready_dt = parse_dt(pickup_ready_at)
            if ready_dt is not None:
                from datetime import timedelta

                storage = (ready_dt + timedelta(days=self.storage_days)).isoformat()
        return Shipment(
            provider=self.key,
            shipment_id=str(dispatch or number or "—"),
            status=str(status_name),
            created_at=created_at,
            expected_at=None,
            delivered_at=delivery_date or None,
            pickup_ready_at=pickup_ready_at,
            storage_expires_at=storage,
            product=None,
            quantity=None,
            destination=city or None,
            pickup_point=city if pickup_ready_at else None,
            tracking=str(dispatch or number or ""),
            status_code=status_code,
            is_delivered=delivered,
            is_cancelled=cancelled,
            extra={"im_number": number, "storage_estimated": bool(pickup_ready_at and not delivered)},
        )

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        if not self.configured():
            raise ShippingError("СДЭК не настроен: заполните CDEK_ACCOUNT и CDEK_SECURE_PASSWORD")
        orders = await self._fetch_v15_orders(since, to)
        orders = orders[: max(1, min(int(limit), 500))]
        if not orders:
            return []

        async def build(order_el: ET.Element) -> Shipment:
            dispatch = order_el.attrib.get("DispatchNumber") or ""
            number = order_el.attrib.get("Number") or ""
            status_el = order_el.find("Status")
            fallback_status = status_el.attrib.get("Description") if status_el is not None else ""
            fallback_date = order_el.attrib.get("DeliveryDate") or ""
            if dispatch:
                async with self._detail_sem:
                    entity = await self._v2_get_order(dispatch)
                if entity:
                    return self._build_from_detail(
                        entity,
                        fallback_number=number,
                        fallback_dispatch=dispatch,
                        fallback_status=fallback_status,
                        fallback_delivery_date=fallback_date,
                    )
            return self._build_from_xml(order_el)

        return list(await asyncio.gather(*(build(el) for el in orders)))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ShippingService:
    def __init__(self, providers: Iterable[DeliveryProvider]):
        self.providers: Dict[str, DeliveryProvider] = {p.key: p for p in providers}

    def get(self, key: str) -> Optional[DeliveryProvider]:
        return self.providers.get(key)

    def list(self) -> List[DeliveryProvider]:
        return list(self.providers.values())

    async def fetch(self, key: str, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        provider = self.get(key)
        if provider is None:
            raise ShippingError(f"Неизвестная служба доставки: {key}")
        return await provider.fetch(since, to, limit=limit)

    async def fetch_all(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        result: List[Shipment] = []
        for provider in self.providers.values():
            if not provider.configured():
                continue
            try:
                result.extend(await provider.fetch(since, to, limit=limit))
            except Exception as e:
                logger.warning("Delivery provider %s failed: %s", provider.key, e)
        return result


def build_shipping_service(
    ozon_client: Any = None,
    yandex_token: str = "",
    cdek_account: str = "",
    cdek_secure: str = "",
    cdek_v1_url: str = "https://integration.cdek.ru",
    cdek_v2_url: str = "https://api.cdek.ru",
    cdek_storage_days: int = 7,
) -> ShippingService:
    return ShippingService(
        [
            OzonDeliveryProvider(ozon_client) if ozon_client is not None else _EmptyProvider("ozon", "Ozon"),
            YandexDeliveryProvider(yandex_token),
            CdekDeliveryProvider(
                cdek_account,
                cdek_secure,
                v1_url=cdek_v1_url,
                v2_url=cdek_v2_url,
                storage_days=cdek_storage_days,
            ),
        ]
    )


class _EmptyProvider(DeliveryProvider):
    def __init__(self, key: str, title: str):
        self.key = key
        self.title = title

    def configured(self) -> bool:
        return False

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        raise ShippingError(f"{self.title} не настроен")
YANDEX_OTHER_STATUS_LABELS = {
    "CREATED": "Создан и подтверждён",
    "VALIDATING_ERROR": "Ошибка подтверждения",
    "DELIVERY_PROCESSING_STARTED": "Создаётся в сортировочном центре",
    "SORTING_CENTER_LOADED": "Подтверждён в сортировочном центре",
    "SORTING_CENTER_AT_START": "Поступил в точку приёма",
    "SORTING_CENTER_PREPARED": "Готов к отправке",
    "SORTING_CENTER_TRANSMITTED": "Доставляется",
    "DELIVERY_AT_START": "Готовится к отправке получателю",
    "DELIVERY_AT_START_SORT": "Готовится к отправке получателю",
    "DELIVERY_TRANSPORTATION_RECIPIENT": "Выдан курьеру",
    "DELIVERY_TRANSPORTATION": "В пути",
    "DELIVERY_ATTEMPT_FAILED": "Неудачная попытка вручения",
    "DELIVERY_TRANSMITTED_TO_RECIPIENT": "Выдан получателю",
    "DELIVERY_ARRIVED_PICKUP_POINT": "Прибыл в пункт назначения",
    "CONFIRMATION_CODE_RECEIVED": "Получен код подтверждения",
    "PARTICULARLY_DELIVERED": "Частично доставлен",
    "DELIVERY_DELIVERED": "Доставлен",
    "CANCELLED": "Отменён",
    "SORTING_CENTER_RETURN_RETURNED": "Возвращён отправителю",
    "RETURN_TRANSPORTATION_STARTED": "Едет в точку выдачи",
    "RETURN_ARRIVED_DELIVERY": "Возвращён на склад",
    "RETURN_READY_FOR_PICKUP": "Готов для передачи магазину",
    "RETURN_RETURNED": "Возвращён в магазин",
    "DELIVERY_TIME_INTERVALS_UPDATED": "Время доставки изменено",
}


def fmt_yandex_dt(value: Any) -> str:
    dt = parse_dt(value)
    if dt is not None:
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    return str(value)


class YandexDeliveryProvider(DeliveryProvider):
    key = "yandex"
    title = "Яндекс Доставка"
    emoji = "🟡"
    EXPRESS_URL = "https://b2b.taxi.yandex.net"
    OTHER_DAY_URL = "https://b2b-authproxy.taxi.yandex.net"

    def __init__(self, token: str):
        self.token = (token or "").strip()

    def configured(self) -> bool:
        return bool(self.token)

    async def fetch(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        if not self.configured():
            raise ShippingError("Яндекс Доставка не настроена: заполните YANDEX_DELIVERY_TOKEN")
        result: List[Shipment] = []
        result.extend(await self._fetch_other_day(since, to, limit))
        express = await self._fetch_express(since, to, limit)
        seen = {s.shipment_id for s in result}
        for shipment in express:
            if shipment.shipment_id not in seen:
                result.append(shipment)
                seen.add(shipment.shipment_id)
        return result[:limit]

    async def _fetch_other_day(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        body = {
            "from": since.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "to": to.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.OTHER_DAY_URL + "/api/b2b/platform/requests/info",
                    headers=headers,
                    json=body,
                )
        except httpx.HTTPError as e:
            raise ShippingError(f"Яндекс Доставка: сетевая ошибка: {e}") from e
        if resp.status_code >= 400:
            raise ShippingError(f"Яндекс Доставка: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ShippingError("Яндекс Доставка: некорректный ответ API") from e
        requests = data.get("requests") if isinstance(data, dict) else None
        if not isinstance(requests, list):
            return []

        point_ids = []
        for req in requests:
            req_data = req.get("request") if isinstance(req.get("request"), dict) else {}
            dest = req_data.get("destination") if isinstance(req_data.get("destination"), dict) else {}
            ps = dest.get("platform_station") if isinstance(dest.get("platform_station"), dict) else {}
            pid = ps.get("platform_id")
            if pid and pid not in point_ids:
                point_ids.append(pid)
        pvz_map: Dict[str, Dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=30.0) as point_client:
                point_resp = await point_client.post(
                    self.OTHER_DAY_URL + "/api/b2b/platform/pickup-points/list",
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    json={"pickup_point_ids": point_ids},
                )
            if point_resp.status_code < 400:
                point_data = point_resp.json()
                for p in point_data.get("points", []) if isinstance(point_data, dict) else []:
                    if isinstance(p, dict) and p.get("id"):
                        pvz_map[str(p["id"])] = p
        except Exception:
            pvz_map = {}

        sem = asyncio.Semaphore(4)

        async def fetch_history(request_id: str) -> List[Dict[str, Any]]:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(
                        self.OTHER_DAY_URL + "/api/b2b/platform/request/history",
                        headers={"Authorization": f"Bearer {self.token}"},
                        params={"request_id": request_id},
                    )
            except httpx.HTTPError:
                return []
            if resp.status_code >= 400:
                return []
            try:
                data = resp.json()
            except ValueError:
                return []
            history = data.get("state_history") if isinstance(data, dict) else None
            return history if isinstance(history, list) else []

        async def build(req: Dict[str, Any]) -> Shipment:
            request = req.get("request") if isinstance(req.get("request"), dict) else {}
            info = request.get("info") if isinstance(request.get("info"), dict) else {}
            state = req.get("state") if isinstance(req.get("state"), dict) else {}
            status_raw = str(state.get("status") or req.get("status") or "").upper()
            status = YANDEX_OTHER_STATUS_LABELS.get(status_raw, status_raw or "—")
            delivered = status_raw in ("DELIVERY_DELIVERED", "DELIVERY_TRANSMITTED_TO_RECIPIENT", "PARTICULARLY_DELIVERED")
            cancelled = status_raw == "CANCELLED" or status_raw.startswith("RETURN_")
            pickup_ready = status_raw == "DELIVERY_ARRIVED_PICKUP_POINT"

            request_id = str(req.get("request_id") or "—")
            operator_id = str(info.get("operator_request_id") or request_id)
            items = request.get("items") if isinstance(request.get("items"), list) else []
            qty = 0
            names: List[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    qty += int(item.get("count") or 1)
                except (TypeError, ValueError):
                    qty += 1
                name = item.get("name")
                if name and str(name) not in names:
                    names.append(str(name))
            product = " / ".join(names[:2]) + ("…" if len(names) > 2 else "") if names else None

            destination = request.get("destination") if isinstance(request.get("destination"), dict) else {}
            dest_type = str(destination.get("type") or "")
            pick_point_id = None
            if dest_type == "platform_station" and isinstance(destination.get("platform_station"), dict):
                pick_point_id = destination["platform_station"].get("platform_id")
            pvz = pvz_map.get(str(pick_point_id), {}) if pick_point_id else {}
            pvz_address = None
            if isinstance(pvz, dict):
                address = pvz.get("address") if isinstance(pvz.get("address"), dict) else {}
                pvz_address = address.get("full_address") or ", ".join(filter(None, [address.get("locality"), address.get("street"), address.get("house")]))
                pvz_name = pvz.get("name")
                pvz_phone = (pvz.get("contact") or {}).get("phone") if isinstance(pvz.get("contact"), dict) else None
            else:
                pvz_name = None
                pvz_phone = None
            destination_text = pvz_address or ("Пункт выдачи Яндекс Доставки" if dest_type == "platform_station" else "Адрес получателя")
            if pvz_name and pvz_address:
                destination_text = f"{pvz_name}: {pvz_address}"
            elif pick_point_id and destination_text == "Пункт выдачи Яндекс Доставки":
                destination_text = f"Пункт выдачи Яндекс Доставки ({pick_point_id})"

            interval = destination.get("interval_utc") if isinstance(destination.get("interval_utc"), dict) else {}
            window = None
            expected_dt = None
            if interval.get("from") and interval.get("to"):
                window = f"{fmt_yandex_dt(interval['from'])}–{fmt_yandex_dt(interval['to'])}"
            if interval.get("to"):
                expected_dt = str(interval["to"])
            elif interval.get("from"):
                expected_dt = str(interval["from"])

            history: List[Dict[str, Any]] = []
            async with sem:
                history = await fetch_history(request_id)

            created_at = None
            delivered_at = None
            pickup_ready_at = None
            latest_ts = None
            for entry in history:
                code = str(entry.get("status") or "").upper()
                ts = entry.get("timestamp_utc") or entry.get("timestamp")
                if code == "CREATED" and created_at is None:
                    created_at = ts
                if code == status_raw and latest_ts is None:
                    latest_ts = ts
                if pickup_ready and code == "DELIVERY_ARRIVED_PICKUP_POINT" and pickup_ready_at is None:
                    pickup_ready_at = ts
                if delivered and code in ("DELIVERY_DELIVERED", "DELIVERY_TRANSMITTED_TO_RECIPIENT", "PARTICULARLY_DELIVERED") and delivered_at is None:
                    delivered_at = ts
            if pickup_ready and not pickup_ready_at:
                pickup_ready_at = state.get("timestamp_utc") or state.get("timestamp")
            if delivered and not delivered_at:
                delivered_at = state.get("timestamp_utc") or state.get("timestamp")
            if not created_at and history:
                created_at = history[0].get("timestamp_utc") or history[0].get("timestamp")

            recipient_info = request.get("recipient_info") if isinstance(request.get("recipient_info"), dict) else {}
            recipient_name = " ".join(filter(None, [recipient_info.get("last_name"), recipient_info.get("first_name"), recipient_info.get("patronymic")]))
            if not recipient_name:
                recipient_name = recipient_info.get("first_name")
            extra = {
                "yandex_api": "other-day",
                "raw_status": status_raw,
                "yandex_platform_destination": pick_point_id,
                "yandex_delivery_window": window,
                "status_time": latest_ts or state.get("timestamp_utc") or state.get("timestamp"),
                "pvz_name": pvz_name if "pvz_name" in locals() else None,
                "pvz_address": pvz_address if "pvz_address" in locals() else None,
                "pvz_phone": pvz_phone if "pvz_phone" in locals() else None,
                "recipient_name": recipient_name,
                "recipient_phone": recipient_info.get("phone"),
                "recipient_email": recipient_info.get("email"),
            }
            return Shipment(
                provider=self.key,
                shipment_id=operator_id,
                status=status,
                created_at=created_at,
                expected_at=expected_dt,
                delivered_at=delivered_at,
                pickup_ready_at=pickup_ready_at,
                product=product,
                quantity=qty or None,
                destination=destination_text,
                pickup_point=pick_point_id,
                tracking=operator_id,
                status_code=status_raw,
                is_delivered=delivered,
                is_cancelled=cancelled,
                extra=extra,
            )

        tasks = [build(req) for req in requests[: max(1, min(int(limit), 1000))]]
        result: List[Shipment] = []
        for shipment in await asyncio.gather(*tasks):
            result.append(shipment)
        return result[:limit]

    async def _fetch_express(self, since: datetime, to: datetime, limit: int = 200) -> List[Shipment]:
        payload = {
            "limit": max(1, min(int(limit), 1000)),
            "offset": 0,
            "created_from": since.isoformat(),
            "created_to": to.isoformat(),
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.EXPRESS_URL + "/b2b/cargo/integration/v2/claims/search",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ShippingError(f"Яндекс Доставка: сетевая ошибка: {e}") from e
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        claims = data.get("claims") if isinstance(data, dict) else None
        if not isinstance(claims, list):
            return []
        result: List[Shipment] = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            status_raw = str(claim.get("status") or "").lower()
            status = YANDEX_STATUS_LABELS.get(status_raw, status_raw or "—")
            delivered = status_raw in ("delivered", "delivered_finish")
            cancelled = status_raw.startswith("cancelled") or status_raw in ("failed", "returned", "returned_finish")
            items = claim.get("items") if isinstance(claim.get("items"), list) else []
            qty = 0
            names: List[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    qty += int(item.get("quantity") or 1)
                except (TypeError, ValueError):
                    qty += 1
                title = item.get("title")
                if title:
                    names.append(str(title))
            product = " / ".join(names[:2]) + ("…" if len(names) > 2 else "") if names else None
            destination = None
            expected = claim.get("due")
            route_points = claim.get("route_points") if isinstance(claim.get("route_points"), list) else []
            for point in route_points:
                if not isinstance(point, dict) or point.get("type") != "destination":
                    continue
                addr = point.get("address") if isinstance(point.get("address"), dict) else {}
                destination = addr.get("fullname") or addr.get("shortname") or destination
                interval = point.get("expected_visit_interval") if isinstance(point.get("expected_visit_interval"), dict) else {}
                expected = expected or interval.get("to") or interval.get("from")
            result.append(
                Shipment(
                    provider=self.key,
                    shipment_id=str(claim.get("id") or "—"),
                    status=str(status),
                    created_at=claim.get("created_ts"),
                    expected_at=expected,
                    delivered_at=None,
                    pickup_ready_at=None,
                    product=product,
                    quantity=qty or None,
                    destination=destination,
                    pickup_point=None,
                    tracking=str(claim.get("shipping_document") or claim.get("id") or ""),
                    status_code=status_raw,
                    is_delivered=delivered,
                    is_cancelled=cancelled,
                    extra={"yandex_api": "express", "raw_status": status_raw},
                )
            )
        return result[:limit]
