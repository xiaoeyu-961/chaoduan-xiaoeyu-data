#!/usr/bin/env python3
"""Collect public A-share market data and publish a stable dashboard payload."""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CN_TZ = timezone(timedelta(hours=8))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
}
INDEX_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&secids=1.000001,0.399001,0.399006,1.000688"
    "&fields=f12,f14,f2,f3,f4,f6,f124"
)
BREADTH_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn={page}&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3"
    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f2,f3,f6"
)
POOL_URLS = {
    "limitUp": "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
    "broken": "https://push2ex.eastmoney.com/getTopicZBPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
    "limitDown": "https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
}


def get_json(url: str, attempts: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=UA, timeout=18)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def date_key(days_ago: int = 0) -> str:
    return (datetime.now(CN_TZ) - timedelta(days=days_ago)).strftime("%Y%m%d")


def fetch_breadth() -> list[dict]:
    rows: list[dict] = []
    total = 5000
    page = 1
    while len(rows) < total and page <= 14:
        body = get_json(BREADTH_URL.format(page=page))
        data = body.get("data") or {}
        batch = data.get("diff") or []
        if not batch:
            break
        rows.extend(batch)
        total = int(number(data.get("total"), len(rows)))
        page += 1
    return rows


def latest_pool(kind: str) -> tuple[str, list[dict]]:
    base = POOL_URLS[kind]
    for offset in range(12):
        day = date_key(offset)
        try:
            pool = get_json(f"{base}&date={day}").get("data", {}).get("pool", [])
            if isinstance(pool, list) and (pool or kind != "limitUp"):
                return day, pool
        except Exception:
            continue
    return "", []


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def normalize_stock(item: dict) -> dict:
    return {
        "code": str(item.get("c") or ""),
        "name": str(item.get("n") or ""),
        "theme": str(item.get("hhy") or item.get("hybk") or "未分类"),
        "height": int(number(item.get("lbc"), 1)),
        "firstLimit": str(item.get("fbt") or ""),
        "lastLimit": str(item.get("lbt") or ""),
        "amount": number(item.get("amount")),
        "turnover": number(item.get("hs")),
        "change": number(item.get("zdp")),
        "sealAmount": number(item.get("fund")),
        "openCount": int(number(item.get("zbc"), 0)),
    }


def build_payload() -> dict:
    errors: list[str] = []
    index_body = get_json(INDEX_URL)
    try:
        breadth_rows = fetch_breadth()
    except Exception as exc:
        breadth_rows = []
        errors.append(f"breadth: {exc}")
    trade_key, up_pool = latest_pool("limitUp")
    if not trade_key or not up_pool:
        raise RuntimeError("No limit-up pool found for recent trading days")

    broken_key, broken_pool = latest_pool("broken")
    down_key, down_pool = latest_pool("limitDown")
    indices = [
        {
            "code": str(x.get("f12") or ""),
            "name": str(x.get("f14") or ""),
            "price": number(x.get("f2")),
            "change": number(x.get("f3")),
            "amount": number(x.get("f6")),
        }
        for x in index_body.get("data", {}).get("diff", [])
    ]

    up = down = flat = 0
    total_amount = 0.0
    for item in breadth_rows:
        change = number(item.get("f3"))
        total_amount += number(item.get("f6"))
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1

    limit_ups = sorted(
        (normalize_stock(x) for x in up_pool),
        key=lambda x: (-x["height"], -x["amount"]),
    )
    broken = [normalize_stock(x) for x in broken_pool] if broken_key == trade_key else []
    limit_down = [normalize_stock(x) for x in down_pool] if down_key == trade_key else []
    highest = limit_ups[0] if limit_ups else None

    theme_map: dict[str, dict] = defaultdict(lambda: {"limitUpCount": 0, "maxHeight": 0, "leaders": []})
    for stock in limit_ups:
        theme = theme_map[stock["theme"]]
        theme["limitUpCount"] += 1
        theme["maxHeight"] = max(theme["maxHeight"], stock["height"])
        if len(theme["leaders"]) < 4:
            theme["leaders"].append({"name": stock["name"], "code": stock["code"], "height": stock["height"]})
    themes = [
        {"name": name, **values}
        for name, values in sorted(
            theme_map.items(),
            key=lambda pair: (-pair[1]["limitUpCount"], -pair[1]["maxHeight"], pair[0]),
        )
    ]

    denominator = len(limit_ups) + len(broken)
    broken_rate = round(len(broken) / denominator * 100, 2) if denominator else 0
    generated = datetime.now(timezone.utc)
    return {
        "schemaVersion": 1,
        "ok": True,
        "source": "GitHub Actions · 东方财富公开行情",
        "sourceMode": "scheduled-snapshot",
        "tradeDate": f"{trade_key[:4]}-{trade_key[4:6]}-{trade_key[6:]}",
        "updatedAt": generated.isoformat().replace("+00:00", "Z"),
        "indices": indices,
        "breadth": {"up": up, "down": down, "flat": flat, "totalAmount": total_amount},
        "limitUpCount": len(limit_ups),
        "brokenCount": len(broken),
        "limitDownCount": len(limit_down),
        "brokenRate": broken_rate,
        "highest": highest,
        "limitUps": limit_ups,
        "broken": broken,
        "limitDown": limit_down,
        "themes": themes[:30],
        "quality": {
            "complete": bool(indices and limit_ups and (up + down + flat) > 1000),
            "errors": errors,
            "collector": "github-actions",
        },
    }


def main() -> None:
    target = Path(os.environ.get("MARKET_OUTPUT", "data/market_latest.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload()
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    print(json.dumps({
        "tradeDate": payload["tradeDate"],
        "limitUpCount": payload["limitUpCount"],
        "brokenCount": payload["brokenCount"],
        "breadthTotal": sum(payload["breadth"][k] for k in ("up", "down", "flat")),
        "complete": payload["quality"]["complete"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
