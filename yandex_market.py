"""Yandex Market Partner API client."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

YandexMarketError = Exception


class YandexMarketClient:
    BASE_URL = "https://api.partner.market.yandex.ru"

    def __init__(self, token: str, campaign_id: Optional[int] = None):
        self.token = (token or "").strip()
        self.campaign_id = int(campaign_id) if campaign_id else None
        self.business_id: Optional[int] = None

    def configured(self) -> bool:
        return bool(self.token)

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        headers = kwargs.pop("headers", {})
        headers.setdefault("Api-Key", self.token)
        headers.setdefault("Content-Type", "application/json")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.request(method, self.BASE_URL + path, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise YandexMarketError(f"Yandex Market: сетевая ошибка: {e}") from e
        if resp.status_code >= 400:
            raise YandexMarketError(f"Yandex Market: HTTP {resp.status_code}: {resp.text[:400]}")
        try:
            return resp.json() if resp.content else {}
        except ValueError as e:
            raise YandexMarketError("Yandex Market: некорректный ответ API") from e

    async def fetch_campaigns(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/v2/campaigns")
        campaigns = data.get("campaigns", [])
        return campaigns if isinstance(campaigns, list) else []

    async def _ensure_context(self) -> None:
        if self.campaign_id is None or self.business_id is None:
            campaigns = await self.fetch_campaigns()
            preferred = None
            for campaign in campaigns:
                if isinstance(campaign, dict):
                    preferred = campaign
                    break
            if not preferred:
                raise YandexMarketError("Yandex Market: не найдено ни одной кампании")
            if self.campaign_id is None:
                self.campaign_id = int(preferred.get("id"))
            if self.business_id is None and isinstance(preferred.get("business"), dict):
                self.business_id = int(preferred["business"].get("id"))
        if self.campaign_id is None:
            raise YandexMarketError("Yandex Market: не задана кампания")

    async def fetch_offers(self, limit: int = 1000) -> List[Dict[str, Any]]:
        await self._ensure_context()
        cid = int(self.campaign_id)
        business_id = self.business_id

        mappings: Dict[str, Dict[str, Any]] = {}
        if business_id is not None:
            page = 1
            while len(mappings) < max(1, int(limit)):
                data = await self._request(
                    "POST",
                    f"/v2/businesses/{business_id}/offer-mappings",
                    json={"page": page, "pageSize": 200},
                )
                result = data.get("result", {}) if isinstance(data, dict) else {}
                rows = result.get("offerMappings", [])
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    offer = row.get("offer", {})
                    if not isinstance(offer, dict):
                        continue
                    oid = offer.get("offerId")
                    if oid is not None:
                        mappings[str(oid)] = offer
                paging = result.get("paging", {}) if isinstance(result.get("paging"), dict) else {}
                pages_count = int(paging.get("totalPages") or paging.get("pagesCount") or 0)
                if pages_count and page >= pages_count:
                    break
                if len(rows) < 200:
                    break
                page += 1

        campaign_offers: Dict[str, Dict[str, Any]] = {}
        page = 1
        while True:
            data = await self._request(
                "POST",
                f"/v2/campaigns/{cid}/offers",
                json={"page": page, "pageSize": 200},
            )
            result = data.get("result", {}) if isinstance(data, dict) else {}
            offers = result.get("offers", [])
            if not isinstance(offers, list) or not offers:
                break
            for offer in offers:
                if isinstance(offer, dict) and offer.get("offerId") is not None:
                    campaign_offers[str(offer["offerId"])] = offer
            paging = result.get("paging", {}) if isinstance(result.get("paging"), dict) else {}
            pages_count = int(paging.get("totalPages") or paging.get("pagesCount") or 0)
            if pages_count and page >= pages_count:
                break
            if len(offers) < 200:
                break
            page += 1
            if page > 50:
                break

        ids = list(dict.fromkeys(list(mappings.keys()) + list(campaign_offers.keys())))
        stock_map: Dict[str, int] = {}
        try:
            stock_map = await self.fetch_stocks(ids[: max(1, int(limit))])
        except Exception:
            stock_map = {}
        result_items: List[Dict[str, Any]] = []
        for oid in ids[: max(1, int(limit))]:
            mapping = mappings.get(oid, {})
            campaign = campaign_offers.get(oid, {})
            basic = campaign.get("basicPrice", {}) if isinstance(campaign.get("basicPrice"), dict) else {}
            price = basic.get("value")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            status_value = campaign.get("status") or ("enabled" if mapping else None)
            if str(status_value or "").upper() == "ARCHIVED":
                continue
            result_items.append(
                {
                    "store": "yandex_market",
                    "offer_id": oid,
                    "name": mapping.get("name") or oid,
                    "category": mapping.get("category"),
                    "status": status_value,
                    "basic_price": price,
                    "currency": basic.get("currencyId"),
                    "updated_at": basic.get("updatedAt"),
                    "stock": stock_map.get(oid),
                }
            )
        return result_items

    async def fetch_warehouses(self) -> List[Dict[str, Any]]:
        await self._ensure_context()
        if self.business_id is None:
            return []
        data = await self._request("POST", f"/v2/businesses/{int(self.business_id)}/warehouses", json={})
        warehouses = data.get("result", {}).get("warehouses", []) if isinstance(data, dict) else []
        return warehouses if isinstance(warehouses, list) else []

    async def fetch_stocks(self, offer_ids: Optional[List[str]] = None) -> Dict[str, int]:
        await self._ensure_context()
        if self.business_id is None:
            return {}
        # Partner warehouse id is usually discovered... use first warehouse from business.
        if not getattr(self, "_partner_warehouse_id", None):
            warehouses = await self.fetch_warehouses()
            first = warehouses[0] if warehouses else None
            if isinstance(first, dict):
                self._partner_warehouse_id = first.get("id")
        wid = getattr(self, "_partner_warehouse_id", None)
        if not wid or not offer_ids:
            return {}
        data = await self._request(
            "POST",
            f"/v3/businesses/{int(self.business_id)}/offers/stocks",
            json={"partnerWarehouseId": int(wid), "offerIds": offer_ids},
        )
        result = data.get("result", {}) if isinstance(data, dict) else {}
        stock_map: Dict[str, int] = {}
        for item in result.get("offers", []) or []:
            if not isinstance(item, dict) or item.get("offerId") is None:
                continue
            available = 0
            for stock in item.get("stocks", []) or []:
                if isinstance(stock, dict) and str(stock.get("type", "")).upper() == "FIT":
                    try:
                        available += int(stock.get("count", 0))
                    except (TypeError, ValueError):
                        continue
            stock_map[str(item.get("offerId"))] = available
        return stock_map

    async def fetch_reviews(self, limit: int = 50) -> List[Dict[str, Any]]:
        await self._ensure_context()
        if self.business_id is None:
            return []
        data = await self._request(
            "POST",
            f"/v2/businesses/{int(self.business_id)}/goods-feedback",
            json={"page": 1, "pageSize": min(100, max(1, int(limit)))},
        )
        result = data.get("result", {}) if isinstance(data, dict) else {}
        feedbacks = result.get("feedbacks", [])
        return feedbacks if isinstance(feedbacks, list) else []

    async def fetch_returns(self, limit: int = 50, days: int = 30) -> List[Dict[str, Any]]:
        await self._ensure_context()
        if self.business_id is None or self.campaign_id is None:
            return []
        date_from = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 30)))).date().isoformat()
        date_to = datetime.now(timezone.utc).date().isoformat()
        body = {
            "businessId": int(self.business_id),
            "campaignIds": [int(self.campaign_id)],
            "dateFrom": date_from,
            "dateTo": date_to,
            "returnType": "RETURN",
            "format": "JSON",
        }
        data = await self._request("POST", "/v2/reports/united-returns/generate", json=body, params={"format": "JSON"})
        report_id = (data.get("result", {}) or {}).get("reportId")
        if not report_id:
            return []
        timeout_at = time.monotonic() + 45.0
        file_url = None
        while time.monotonic() < timeout_at:
            info = await self._request("GET", f"/v2/reports/info/{report_id}", params={"sourceType": "SELLER"})
            res = info.get("result", {}) if isinstance(info, dict) else {}
            status = str(res.get("status") or "")
            if status in ("DONE", "FAILED", "NO_DATA"):
                file_url = res.get("file")
                break
            await asyncio.sleep(2.0)
        if not file_url:
            return []
        async with httpx.AsyncClient(timeout=60.0) as c:
            resp = await c.get(file_url)
        if resp.status_code >= 400:
            return []
        records: List[Dict[str, Any]] = []
        content = resp.content
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for info in archive.infolist():
                    name = str(info.filename or "")
                    if info.is_dir():
                        continue
                    lower = name.lower()
                    raw = archive.read(info)
                    if lower.endswith(".json"):
                        try:
                            payload = json.loads(raw.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            payload = None
                        if payload is not None:
                            self._append_return_records(payload, records)
                    elif lower.endswith(".csv"):
                        try:
                            text = raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            text = raw.decode("utf-8", errors="replace")
                        for row in csv.DictReader(io.StringIO(text)):
                            records.append(row)
        except zipfile.BadZipFile:
            # Some API responses may already be raw JSON.
            try:
                payload = json.loads(content.decode("utf-8-sig"))
                self._append_return_records(payload, records)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return records[: max(1, int(limit or 50))]

    @staticmethod
    def _append_return_records(payload: Any, records: List[Dict[str, Any]]) -> None:
        if isinstance(payload, list):
            records.extend(item for item in payload if isinstance(item, dict))
            return
        if not isinstance(payload, dict):
            return
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                records.extend(item for item in value if isinstance(item, dict))
                return
        for key in ("data", "result", "items"):
            value = payload.get(key)
            if isinstance(value, dict):
                for nested in value.values():
                    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                        records.extend(item for item in nested if isinstance(item, dict))
                        return

    async def fetch_orders(self, limit: int = 50) -> List[Dict[str, Any]]:
        await self._ensure_context()
        cid = int(self.campaign_id)
        try:
            data = await self._request("GET", f"/v2/campaigns/{cid}/orders")
        except YandexMarketError:
            return []
        orders = data.get("orders", []) if isinstance(data, dict) else []
        if not isinstance(orders, list):
            return []
        return orders[: max(1, min(int(limit), 200))]
