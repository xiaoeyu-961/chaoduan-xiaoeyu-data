#!/usr/bin/env python3
"""Collect raw short-term sentiment data without assigning cycle labels.

The collector deliberately separates facts from later model judgments.  It
stores daily structure, next-session feedback, promotion rates, sector breadth
and an intraday timeline.  Missing fields stay null and are listed explicitly.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from data_checks import intraday_quality
from urllib.parse import quote

from fetch_market import CN_TZ, POOL_URLS, get_json, normalize_stock, number
from hithink_client import get as hithink_get, pool as hithink_pool
from fetch_hithink import normalize_stock as hithink_stock

DATA_DIR = Path("data")
MARKET_FILE = DATA_DIR / "market_latest.json"
EMOTION_FILE = DATA_DIR / "emotion_latest.json"
HISTORY_FILE = DATA_DIR / "emotion_history.json"
INTRADAY_FILE = DATA_DIR / "intraday_latest.json"
QUOTE_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&fields=f12,f14,f2,f3,f6&secids={secids}"
)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def day_key(value: str) -> str:
    return value.replace("-", "")


def fetch_pool(kind: str, date: str) -> list[dict]:
    # Historical backfill favors a fast explicit miss over multiplying retries
    # across dozens of dates.  The next scheduled run can fill transient gaps.
    body = get_json(f"{POOL_URLS[kind]}&date={day_key(date)}", attempts=1)
    pool = (body.get("data") or {}).get("pool") or []
    return pool if isinstance(pool, list) else []


def collect_sessions(end_date: str, target: int = 20) -> list[dict]:
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    dates = [(end - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(40)]
    up_by_date: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_pool, "limitUp", date): date for date in dates}
        for future in as_completed(futures):
            date = futures[future]
            try:
                rows = future.result()
                if rows:
                    up_by_date[date] = rows
            except Exception:
                continue
    selected = sorted(up_by_date)[-target:]
    auxiliary: dict[tuple[str, str], list[dict]] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_pool, kind, date): (kind, date)
            for date in selected
            for kind in ("broken", "limitDown")
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                auxiliary[key] = future.result()
            except Exception:
                auxiliary[key] = []
    sessions: list[dict] = []
    for date in selected:
        up = up_by_date[date]
        broken = auxiliary.get(("broken", date), [])
        down = auxiliary.get(("limitDown", date), [])
        stocks = [normalize_stock(row) for row in up]
        height_counts = Counter(int(row["height"]) for row in stocks)
        denominator = len(stocks) + len(broken)
        sessions.append({
            "date": date,
            "limit_up_count": len(stocks),
            "broken_count": len(broken),
            "limit_down_count": len(down),
            "seal_rate_pct": round(len(stocks) / denominator * 100, 2) if denominator else None,
            "highest_board": max(height_counts, default=0),
            "height_counts": {str(key): height_counts[key] for key in sorted(height_counts)},
            "stocks": stocks,
        })
    return sessions


def collect_hithink_sessions(end_date: str, today: dict, target: int = 8) -> list[dict]:
    """Build a source-consistent recent history; unavailable dates stay missing."""
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    sessions = []
    for offset in reversed(range(12)):
        date = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        if date == end_date:
            up = today.get("limitUps") or []
            broken_count = today.get("brokenCount")
            down_count = today.get("limitDownCount")
        else:
            try:
                up = [hithink_stock(row) for row in hithink_pool("limitUp", date)]
                if not up:
                    continue
                broken_count = len(hithink_pool("broken", date))
                down_count = len(hithink_pool("limitDown", date))
            except Exception:
                continue
        if not up or broken_count is None or down_count is None:
            continue
        heights = Counter(int(r["height"]) for r in up if r.get("height") is not None)
        total = len(up) + broken_count
        sessions.append({"date": date, "limit_up_count": len(up),
                         "broken_count": broken_count, "limit_down_count": down_count,
                         "seal_rate_pct": round(len(up) / total * 100, 2) if total else None,
                         "highest_board": max(heights, default=0),
                         "height_counts": {str(k): v for k, v in heights.items()},
                         "stocks": up})
    return sessions[-target:]


def hithink_quotes(codes: list[str]) -> dict[str, dict]:
    result = {}
    for start in range(0, len(codes), 100):
        batch = codes[start:start + 100]
        if not batch:
            continue
        try:
            data = hithink_get("/api/a-share/prices/snapshot", {
                "thscodes": ",".join(("%s.SH" if c.startswith(("6", "9")) else
                                      "%s.BJ" if c.startswith(("4", "8")) else "%s.SZ") % c
                                     for c in batch)})
        except Exception:
            continue
        for row in data.get("item") or []:
            code = row.get("ticker")
            result[code] = {"code": code, "price": row.get("last_price"),
                            "change_pct": row.get("price_change_ratio_pct"),
                            "amount": row.get("turnover")}
    return result


def promotion(prev: dict, current: dict) -> dict:
    current_map = {row["code"]: row for row in current["stocks"]}
    by_height: dict[str, dict] = {}
    promoted_total = 0
    for height in sorted({int(row["height"]) for row in prev["stocks"]}):
        eligible = [row for row in prev["stocks"] if int(row["height"]) == height]
        promoted = [
            row for row in eligible
            if row["code"] in current_map
            and int(current_map[row["code"]]["height"]) >= height + 1
        ]
        promoted_total += len(promoted)
        by_height[str(height)] = {
            "eligible": len(eligible),
            "promoted": len(promoted),
            "rate_pct": round(len(promoted) / len(eligible) * 100, 2) if eligible else None,
        }
    total = len(prev["stocks"])
    return {
        "eligible": total,
        "promoted": promoted_total,
        "rate_pct": round(promoted_total / total * 100, 2) if total else None,
        "by_height": by_height,
    }


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    unique = list(dict.fromkeys(code for code in codes if code))
    for start in range(0, len(unique), 30):
        batch = unique[start:start + 30]
        try:
            body = get_json(
                QUOTE_URL.format(secids=quote(",".join(secid(code) for code in batch), safe=",.")),
                attempts=2,
            )
        except Exception:
            # Quote feedback is an enrichment field. A transient upstream error
            # must not block the factual pool snapshot from being published.
            continue
        for row in (body.get("data") or {}).get("diff") or []:
            code = str(row.get("f12") or "")
            result[code] = {
                "code": code,
                "name": str(row.get("f14") or ""),
                "price": number(row.get("f2"), None),
                "change_pct": number(row.get("f3"), None),
                "amount": number(row.get("f6"), None),
            }
    return result


def feedback(rows: list[dict], quotes: dict[str, dict]) -> dict:
    changes = [
        quotes[row["code"]]["change_pct"]
        for row in rows
        if row["code"] in quotes and quotes[row["code"]]["change_pct"] is not None
    ]
    if not changes:
        return {
            "sample": 0, "avg_change_pct": None, "median_change_pct": None,
            "positive_rate_pct": None, "loss_below_minus_5_rate_pct": None,
        }
    return {
        "sample": len(changes),
        "avg_change_pct": round(statistics.fmean(changes), 2),
        "median_change_pct": round(statistics.median(changes), 2),
        "positive_rate_pct": round(sum(x > 0 for x in changes) / len(changes) * 100, 2),
        "loss_below_minus_5_rate_pct": round(sum(x <= -5 for x in changes) / len(changes) * 100, 2),
    }


def theme_structure(sessions: list[dict]) -> list[dict]:
    if not sessions:
        return []
    current = sessions[-1]
    recent = sessions[-3:]
    recent_counts: list[Counter] = []
    for session in recent:
        recent_counts.append(Counter(row["theme"] for row in session["stocks"]))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in current["stocks"]:
        grouped[row["theme"]].append(row)
    result = []
    for theme, rows in grouped.items():
        heights = Counter(int(row["height"]) for row in rows)
        active_days = sum(counter.get(theme, 0) > 0 for counter in recent_counts)
        result.append({
            "name": theme,
            "limit_up_count": len(rows),
            "first_board_count": heights.get(1, 0),
            "second_board_count": heights.get(2, 0),
            "third_board_count": heights.get(3, 0),
            "high_board_count": sum(count for height, count in heights.items() if height >= 4),
            "max_height": max(heights, default=0),
            "active_days_3": active_days,
            "leaders": [
                {"code": row["code"], "name": row["name"], "height": row["height"]}
                for row in sorted(rows, key=lambda item: (-item["height"], -item["amount"]))[:4]
            ],
        })
    return sorted(result, key=lambda row: (-row["limit_up_count"], -row["max_height"], row["name"]))[:30]


def append_intraday(market: dict, promotion_data: dict | None) -> dict:
    trade_date = market["tradeDate"]
    old = read_json(INTRADAY_FILE, {})
    snapshots = (old.get("snapshots", []) if old.get("trade_date") == trade_date
                 and old.get("source") == market.get("source") else [])
    timestamp = market.get("updatedAt") or datetime.now(CN_TZ).isoformat()
    minute = timestamp[:16]
    snapshot = {
        "time": timestamp,
        "limit_up_count": market.get("limitUpCount"),
        "limit_down_count": market.get("limitDownCount"),
        "broken_count": market.get("brokenCount"),
        "broken_rate_pct": market.get("brokenRate"),
        "promotion_rate_pct": promotion_data.get("rate_pct") if promotion_data else None,
        "highest_board": (market.get("highest") or {}).get("height"),
        "leader": {
            "code": (market.get("highest") or {}).get("code"),
            "name": (market.get("highest") or {}).get("name"),
        } if market.get("highest") else None,
        "strongest_theme": (market.get("themes") or [{}])[0].get("name"),
    }
    snapshots = [row for row in snapshots if str(row.get("time", ""))[:16] != minute]
    snapshots.append(snapshot)
    snapshots.sort(key=lambda row: row["time"])
    payload = {
        "schema_version": 1,
        "ok": True,
        "trade_date": trade_date,
        "source": market.get("source"),
        "snapshots": snapshots[-80:],
        "data_quality": {
            "snapshot_count": len(snapshots[-80:]),
            "timeline_ready": len(snapshots) >= 6,
            "note": "至少6个盘中快照后才允许判断完整日内路径。",
        },
    }
    _, payload['data_quality'] = intraday_quality(payload['snapshots'], trade_date)
    write_json(INTRADAY_FILE, payload)
    return payload


def main() -> None:
    market = read_json(MARKET_FILE, {})
    previous_payload = read_json(EMOTION_FILE, {})
    if not market.get("ok") or not market.get("tradeDate"):
        raise RuntimeError("market_latest.json is unavailable")
    hithink = market.get("source") == "同花顺官方 Financial-API"
    sessions = (collect_hithink_sessions(market["tradeDate"], market) if hithink
                else collect_sessions(market["tradeDate"], 20))
    if not sessions:
        raise RuntimeError("No sentiment sessions collected")

    promotion_data = promotion(sessions[-2], sessions[-1]) if len(sessions) >= 2 else None
    previous = sessions[-2] if len(sessions) >= 2 else None
    quote_map = (hithink_quotes([row["code"] for row in previous["stocks"]]) if hithink
                 else fetch_quotes([row["code"] for row in previous["stocks"]])) if previous else {}
    previous_feedback = feedback(previous["stocks"], quote_map) if previous else feedback([], {})
    high_feedback = feedback([row for row in previous["stocks"] if row["height"] >= 4], quote_map) if previous else feedback([], {})
    mid_feedback = feedback([row for row in previous["stocks"] if 2 <= row["height"] <= 3], quote_map) if previous else feedback([], {})
    low_feedback = feedback([row for row in previous["stocks"] if row["height"] == 1], quote_map) if previous else feedback([], {})
    if (previous_payload.get("trade_date") == market["tradeDate"]
            and previous_payload.get("source") == market.get("source")):
        old_ecology = previous_payload.get("market_ecology") or {}
        for current_value, key in (
            (previous_feedback, "previous_limit_up_feedback"),
            (high_feedback, "high_position_feedback"),
            (mid_feedback, "middle_position_feedback"),
            (low_feedback, "first_board_feedback"),
        ):
            old_value = old_ecology.get(key) or {}
            if current_value["sample"] == 0 and old_value.get("sample", 0) > 0:
                current_value.update(old_value)
                current_value["carried_forward"] = True
                current_value["as_of"] = previous_payload.get("updated_at")
    breadth = market.get("breadth") or {}
    breadth_total = sum(int(number(breadth.get(key))) for key in ("known_up", "known_down", "known_flat")) if hithink else sum(int(number(breadth.get(key))) for key in ("up", "down", "flat"))
    width = {
        "available": breadth.get('complete') is True,
        "up": breadth.get("up") if breadth.get('complete') is True else None,
        "down": breadth.get("down") if breadth.get('complete') is True else None,
        "flat": breadth.get("flat") if breadth.get('complete') is True else None,
        "sample": breadth_total,
    }
    missing = []
    if not width["available"]:
        missing.append("全市场上涨/下跌家数")
    if previous_feedback["sample"] == 0:
        missing.append("昨日涨停次日反馈")
    if hithink:
        missing.append("概念板块成分股涨停扩散与持续性")
        if len(sessions) < 20:
            missing.append("同源20交易日情绪序列")
    intraday = append_intraday(market, promotion_data)
    if not intraday["data_quality"]["timeline_ready"]:
        missing.append("完整日内情绪时间轴")

    latest = sessions[-1]
    ecology = {
        "limit_up_count": latest["limit_up_count"],
        "limit_down_count": latest["limit_down_count"],
        "broken_count": latest["broken_count"],
        "broken_rate_pct": round(100 - latest["seal_rate_pct"], 2) if latest["seal_rate_pct"] is not None else None,
        "seal_rate_pct": latest["seal_rate_pct"],
        "highest_board": latest["highest_board"],
        "promotion": promotion_data,
        "previous_limit_up_feedback": previous_feedback,
        "high_position_feedback": high_feedback,
        "middle_position_feedback": mid_feedback,
        "first_board_feedback": low_feedback,
        "market_width": width,
    }
    leaders = [
        {
            "code": row["code"], "name": row["name"], "theme": row["theme"],
            "height": row["height"], "first_limit": row["firstLimit"],
            "last_limit": row["lastLimit"], "open_count": row["openCount"],
            "seal_amount": row["sealAmount"], "amount": row["amount"],
        }
        for row in sorted(latest["stocks"], key=lambda item: (-item["height"], item["firstLimit"]))[:10]
    ]
    payload = {
        "schema_version": 1,
        "ok": True,
        "trade_date": market["tradeDate"],
        "updated_at": market.get("updatedAt"),
        "source": market.get("source"),
        "principle": "仅保存事实数据，不在采集层生成周期标签或主观评分。",
        "market_ecology": ecology,
        "leader_candidates": leaders,
        "themes": [] if hithink else theme_structure(sessions),
        "intraday": {
            "snapshot_count": intraday["data_quality"]["snapshot_count"],
            "timeline_ready": intraday["data_quality"]["timeline_ready"],
        },
        "data_quality": {
            "daily_sessions": len(sessions),
            "complete": not missing,
            "missing": missing,
            "confidence": "high" if not missing else ("medium" if len(missing) <= 2 else "low"),
        },
    }
    history = {
        "schema_version": 1,
        "ok": True,
        "updated_at": market.get("updatedAt"),
        "sessions": [
            {key: value for key, value in session.items() if key != "stocks"}
            for session in sessions
        ],
    }
    write_json(EMOTION_FILE, payload)
    write_json(HISTORY_FILE, history)
    print(json.dumps({
        "trade_date": payload["trade_date"],
        "sessions": len(sessions),
        "promotion_rate_pct": promotion_data.get("rate_pct") if promotion_data else None,
        "previous_feedback_sample": previous_feedback["sample"],
        "intraday_snapshots": intraday["data_quality"]["snapshot_count"],
        "missing": missing,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
