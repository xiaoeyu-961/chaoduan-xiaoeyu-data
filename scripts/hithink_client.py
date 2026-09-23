"""Small read-only client for the official Tonghuashun Financial API.

All errors fail closed. Secrets are only read from the process environment.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import requests

CN_TZ = timezone(timedelta(hours=8))
BASE = "https://fuyao.aicubes.cn"


class HithinkError(RuntimeError):
    pass


def client_key() -> str:
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if not key:
        raise HithinkError("HITHINK_FINANCE_API_KEY is not configured")
    return key


def get(path: str, params: dict | None = None) -> dict:
    if not path.startswith("/api/"):
        raise ValueError("Only API paths are supported")
    key = client_key()
    for attempt in range(3):
        try:
            response = requests.get(BASE + path, params=params,
                                    headers={"X-api-key": key}, timeout=18)
            if response.status_code == 429 or response.status_code >= 500:
                raise HithinkError(f"service HTTP {response.status_code}")
            response.raise_for_status()
            body = response.json()
            code = body.get("code")
            if code == 0:
                if not isinstance(body.get("data"), dict):
                    raise HithinkError("success response missing data")
                return body["data"]
            if code not in (4001, 5001, 5002, 5003):
                raise HithinkError(f"API code {code}: {body.get('message', '')[:120]}")
            raise HithinkError(f"API retryable code {code}")
        except (requests.RequestException, ValueError, HithinkError) as exc:
            # Bad credentials, permissions and invalid parameters must not loop.
            if isinstance(exc, HithinkError) and "API code " in str(exc):
                raise
            if attempt == 2:
                raise HithinkError(f"{path}: {type(exc).__name__}: {str(exc)[:120]}") from None
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")


def date_ms(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)


def pool(kind: str, date: str) -> list[dict]:
    paths = {"limitUp": "limit-up-pool", "broken": "limit-break-pool",
             "limitDown": "limit-down-pool"}
    if kind not in paths:
        raise ValueError(kind)
    path = "/api/a-share/special-data/" + paths[kind]
    first = get(path, {"date_ms": date_ms(date), "page": 1, "size": 200})
    paging = first.get("pagination") or {}
    total, pages = paging.get("total"), paging.get("pages")
    if not isinstance(total, int) or not isinstance(pages, int) or pages < 0:
        raise HithinkError(f"{kind}: missing pagination")
    if total == 0 and pages in (0, 1) and not first.get("item"):
        return []
    if total > 0 and pages < 1:
        raise HithinkError(f"{kind}: invalid page count")
    rows = list(first.get("item") or [])
    for page in range(2, pages + 1):
        result = get(path, {"date_ms": date_ms(date), "page": page, "size": 200})
        if (result.get("pagination") or {}).get("total") != total:
            raise HithinkError(f"{kind}: pool changed during pagination")
        rows.extend(result.get("item") or [])
    codes = [x.get("thscode") for x in rows]
    if len(rows) != total or len(set(codes)) != total or not all(codes):
        raise HithinkError(f"{kind}: incomplete or duplicate pool {len(rows)}/{total}")
    return rows


def all_quotes(limit: int = 100) -> tuple[list[dict], int]:
    """Read every page. The API's total is a code count, not active quote count."""
    rows: list[dict] = []
    total: int | None = None
    for offset in range(0, 10000, limit):
        data = get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        batch = data.get("item")
        if not isinstance(batch, list) or not isinstance(data.get("total"), int):
            raise HithinkError("market snapshot missing pagination fields")
        if total is None:
            total = data["total"]
        elif data["total"] != total:
            raise HithinkError("stock universe changed during pagination")
        rows.extend(batch)
        if offset + limit >= total:
            break
    if total is None or len(rows) != total:
        raise HithinkError(f"incomplete market snapshot: {len(rows)}/{total}")
    codes = [r.get("thscode") for r in rows]
    if len(set(codes)) != total or not all(codes):
        raise HithinkError("market snapshot contains duplicate or missing symbols")
    return rows, total
