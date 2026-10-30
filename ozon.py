"""Ozon Seller API client helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api-seller.ozon.ru"

LIST_PATHS = ("/v3/product/list", "/v2/product/list")
INFO_PATHS = ("/v3/product/info/list", "/v2/product/info/list")
PRICE_PATHS = (
    "/v5/product/info/prices",
    "/v4/product/info/prices",
    "/v3/product/info/prices",
    "/v2/product/info/prices",
)


class OzonError(Exception):
    pass


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(" ", "").replace(",", ".")
        # strip currency symbols
        for ch in ("₽", "руб.", "руб", "RUB", "rub"):
            s = s.replace(ch, "")
        s = s.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("price", "value", "amount", "current", "marketing_price", "seller_price"):
            if key in value:
                r = to_float(value[key])
                if r is not None:
                    return r
        for v in value.values():
            r = to_float(v)
            if r is not None:
                return r
    if isinstance(value, (list, tuple)):
        for v in value:
            r = to_float(v)
            if r is not None:
                return r
    return None


def extract_price(item: Dict[str, Any]) -> Optional[float]:
    """Try to extract a numeric price from an Ozon prices item.
    Prefer the site/marketing seller price (Ozon may change it via promos)."""
    # Direct fields first (some versions expose them directly)
    for key in ("marketing_seller_price", "marketing_price", "price", "current_price", "seller_price"):
        v = item.get(key)
        r = to_float(v)
        if r is not None:
            return r

    # Nested price object
    for key in ("price", "prices"):
        v = item.get(key)
        if isinstance(v, dict):
            for k in ("marketing_seller_price", "marketing_price", "price", "marketing_price", "min_price", "seller_price", "current_price", "value"):
                r = to_float(v.get(k))
                if r is not None:
                    return r
            r = to_float(v)
            if r is not None:
                return r

    # Top-level marketing_price object
    v = item.get("marketing_price")
    if isinstance(v, dict):
        for k in ("marketing_seller_price", "price", "marketing_price", "value", "current"):
            r = to_float(v.get(k))
            if r is not None:
                return r
        r = to_float(v)
        if r is not None:
            return r

    # Last resort: any key containing price
    for k, v in item.items():
        if "price" in str(k).lower():
            r = to_float(v)
            if r is not None:
                return r
    return None


def chunks(seq: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(seq), size):
        yield list(seq[i:i + size])


class OzonClient:
    def __init__(self, client_id: str = "", api_key: str = ""):
        self.client_id = (client_id or "").strip()
        self.api_key = (api_key or "").strip()

    def configured(self) -> bool:
        return bool(self.client_id and self.api_key)

    async def _request(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.configured():
            raise OzonError("Ozon API не настроен: заполните OZON_CLIENT_ID и OZON_API_KEY в .env")
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    BASE_URL + path,
                    headers={
                        "Client-Id": self.client_id,
                        "Api-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.HTTPError as e:
                raise OzonError(f"Сетевая ошибка Ozon API: {e}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise OzonError(f"Ozon API {path}: HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception as e:
            raise OzonError(f"Ozon API {path}: некорректный JSON: {e}") from e
        return data if isinstance(data, dict) else {"data": data}

    async def _post_first_ok(self, paths, payloads) -> (str, Dict[str, Any]):
        last_error: Optional[OzonError] = None
        for path in paths:
            for payload in payloads:
                try:
                    data = await self._request(path, payload)
                    return path, data
                except OzonError as e:
                    last_error = e
                    continue
        if last_error is not None:
            raise last_error
        raise OzonError("Ozon API: все попытки завершились ошибкой")

    # ---------- product list ----------
    async def list_products(self) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        seen = set()
        last_id = ""
        while True:
            payloads = (
                {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000},
                {"filter": {"visibility": "ALL", "product_id": [], "offer_id": []}, "last_id": last_id, "limit": 1000},
                {"last_id": last_id, "limit": 1000},
            )
            try:
                _, data = await self._post_first_ok(LIST_PATHS, payloads)
            except OzonError as e:
                raise OzonError(f"Не удалось получить список товаров: {e}") from e

            result = data.get("result", data)
            items = result.get("items") if isinstance(result, dict) else None
            if items is None:
                items = data.get("items", [])
            if not isinstance(items, list):
                items = []

            if not items and not last_id:
                # maybe empty store
                break

            for it in items:
                if not isinstance(it, dict):
                    continue
                pid_raw = it.get("product_id", it.get("id", it.get("sku")))
                if pid_raw is None:
                    continue
                try:
                    pid = int(pid_raw)
                except (ValueError, TypeError):
                    continue
                if pid in seen:
                    continue
                seen.add(pid)
                collected.append({"product_id": pid, "offer_id": it.get("offer_id")})

            new_last_id = ""
            if isinstance(result, dict):
                new_last_id = result.get("last_id") or ""
            if not new_last_id:
                new_last_id = data.get("last_id") or ""
            total = None
            if isinstance(result, dict):
                total = result.get("total")
            if total is None:
                total = data.get("total")

            if not new_last_id:
                break
            if total is not None:
                try:
                    if len(collected) >= int(total):
                        break
                except (ValueError, TypeError):
                    pass
            if len(items) < 100:
                break
            last_id = new_last_id

        return collected

    # ---------- product info ----------
    async def get_product_info(self, product_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        result: Dict[int, Dict[str, Any]] = {}
        if not product_ids:
            return result
        for chunk in chunks(product_ids, 500):
            payloads = (
                {"product_id": list(chunk), "offer_id": [], "sku": []},
                {"product_ids": list(chunk)},
            )
            try:
                _, data = await self._post_first_ok(INFO_PATHS, payloads)
            except OzonError:
                continue
            res = data.get("result", data)
            items = res.get("items") if isinstance(res, dict) else None
            if items is None:
                items = data.get("items", [])
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid_raw = it.get("product_id", it.get("id", it.get("sku")))
                if pid_raw is None:
                    continue
                try:
                    pid = int(pid_raw)
                except (ValueError, TypeError):
                    continue
                name = it.get("name") or it.get("product_name") or it.get("title")
                offer_id = it.get("offer_id")
                result[pid] = {"name": name, "offer_id": offer_id}
        return result

    # ---------- prices ----------
    async def _fetch_prices_page(self, product_ids: Optional[List[int]], last_id: str):
        if product_ids:
            base_filter = {"visibility": "ALL", "product_id": list(product_ids)}
        else:
            base_filter = {"visibility": "ALL"}
        payloads = (
            {"filter": {**base_filter, "offer_id": []}, "last_id": last_id, "limit": 1000},
            {"filter": base_filter, "last_id": last_id, "limit": 1000},
            {"product_id": list(product_ids) if product_ids else [], "last_id": last_id, "limit": 1000},
        )
        return await self._post_first_ok(PRICE_PATHS, payloads)

    async def fetch_prices(self, product_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        seen = set()
        last_id = ""
        while True:
            try:
                _, data = await self._fetch_prices_page(product_ids, last_id)
            except OzonError as e:
                if not collected:
                    raise OzonError(f"Не удалось получить цены: {e}") from e
                break

            result = data.get("result", data)
            items = result.get("items") if isinstance(result, dict) else None
            if items is None:
                items = data.get("items", [])
            if not isinstance(items, list):
                items = []

            for it in items:
                if not isinstance(it, dict):
                    continue
                pid_raw = it.get("product_id", it.get("id", it.get("sku")))
                if pid_raw is None:
                    continue
                try:
                    pid = int(pid_raw)
                except (ValueError, TypeError):
                    continue
                dedup_key = (pid, last_id)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                collected.append(
                    {
                        "product_id": pid,
                        "offer_id": it.get("offer_id"),
                        "price": extract_price(it),
                        "raw": it,
                    }
                )

            new_last_id = ""
            if isinstance(result, dict):
                new_last_id = result.get("last_id") or ""
            if not new_last_id:
                new_last_id = data.get("last_id") or ""
            total = None
            if isinstance(result, dict):
                total = result.get("total")
            if total is None:
                total = data.get("total")

            if not items:
                break
            if not new_last_id:
                break
            if total is not None:
                try:
                    if len(collected) >= int(total):
                        break
                except (ValueError, TypeError):
                    pass
            if len(items) < 100:
                break
            last_id = new_last_id

        # dedup final by product_id (keep last)
        by_pid: Dict[int, Dict[str, Any]] = {}
        for it in collected:
            by_pid[it["product_id"]] = it
        return list(by_pid.values())

    async def fetch_stocks(self, product_ids: Optional[List[int]] = None) -> Dict[int, Dict[str, Any]]:
        """Return mapping product_id -> {'fbo': int|None, 'fbs': int|None, 'offer_id': ...}."""
        result: Dict[int, Dict[str, Any]] = {}
        ids = list(product_ids or [])
        # If no ids provided, caller should pass product list; here just return empty
        if not ids:
            return result
        for chunk in chunks(ids, 100):
            try:
                data = await self._request(
                    "/v4/product/info/stocks",
                    {"filter": {"product_id": list(chunk)}, "limit": 1000},
                )
            except OzonError as e:
                logger.warning("fetch_stocks chunk failed: %s", e)
                continue
            for it in data.get("items", []) or []:
                if not isinstance(it, dict):
                    continue
                pid_raw = it.get("product_id")
                if pid_raw is None:
                    continue
                try:
                    pid = int(pid_raw)
                except (ValueError, TypeError):
                    continue
                stocks = {}
                for s in it.get("stocks", []) or []:
                    if not isinstance(s, dict):
                        continue
                    stype = str(s.get("type", "")).lower()
                    try:
                        present = int(s.get("present"))
                    except (TypeError, ValueError):
                        present = None
                    try:
                        reserved = int(s.get("reserved"))
                    except (TypeError, ValueError):
                        reserved = None
                    stocks[stype] = present
                    stocks[f"{stype}_reserved"] = reserved
                    if "sku" in s and "sku" not in stocks:
                        stocks["sku"] = s.get("sku")
                result[pid] = {
                    "product_id": pid,
                    "offer_id": it.get("offer_id"),
                    "fbo": stocks.get("fbo"),
                    "fbs": stocks.get("fbs"),
                    "sku": stocks.get("sku"),
                    "fbo_reserved": stocks.get("fbo_reserved"),
                    "fbs_reserved": stocks.get("fbs_reserved"),
                }
        return result

    async def fetch_returns(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            lim = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            lim = 100
        try:
            data = await self._request("/v1/returns/list", {"limit": lim})
        except OzonError as e:
            raise OzonError(f"Не удалось получить возвраты: {e}") from e
        returns = data.get("returns", [])
        if not isinstance(returns, list):
            return []
        return returns

    async def fetch_reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            lim = max(20, min(int(limit or 100), 100))
        except (TypeError, ValueError):
            lim = 100
        try:
            data = await self._request("/v2/review/list", {"limit": lim, "sort": "desc"})
        except OzonError as e:
            raise OzonError(f"Не удалось получить отзывы: {e}") from e
        reviews = data.get("reviews", [])
        if not isinstance(reviews, list):
            return []
        return reviews

    async def get_names_by_sku(self, skus: List[int]) -> Dict[int, Dict[str, Any]]:
        result: Dict[int, Dict[str, Any]] = {}
        ids = [int(x) for x in (skus or []) if x is not None]
        for chunk in chunks(ids, 100):
            try:
                data = await self._request(
                    "/v3/product/info/list",
                    {"product_id": [], "offer_id": [], "sku": list(chunk)},
                )
            except OzonError:
                continue
            items = (data.get("result", data).get("items") if isinstance(data.get("result"), dict) else data.get("items", [])) or []
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                sku = it.get("sku")
                if sku is None:
                    continue
                try:
                    sku_int = int(sku)
                except (ValueError, TypeError):
                    continue
                result[sku_int] = {"name": it.get("name"), "offer_id": it.get("offer_id")}
        return result

    async def fetch_postings_fbs(self, dt_from: str, dt_to: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            lim = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            lim = 50
        try:
            data = await self._request(
                "/v4/posting/fbs/list",
                {
                    "dir": "DESC",
                    "filter": {"since": dt_from, "to": dt_to},
                    "limit": lim,
                    "offset": 0,
                    "with": {"analytics_data": True, "financial_data": True},
                },
            )
        except OzonError as e:
            logger.warning("fetch_postings_fbs failed: %s", e)
            return []
        posts = data.get("postings", [])
        return posts if isinstance(posts, list) else []

    async def fetch_postings_fbo(self, dt_from: str, dt_to: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            lim = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            lim = 50
        try:
            data = await self._request(
                "/v3/posting/fbo/list",
                {
                    "dir": "DESC",
                    "filter": {"since": dt_from, "to": dt_to},
                    "limit": lim,
                    "offset": 0,
                    "with": {"analytics_data": True, "financial_data": True},
                },
            )
        except OzonError as e:
            logger.warning("fetch_postings_fbo failed: %s", e)
            return []
        posts = data.get("postings", [])
        return posts if isinstance(posts, list) else []
