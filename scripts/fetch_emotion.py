#!/usr/bin/env python3
"""Collect raw short-term sentiment data without assigning cycle labels.

The collector deliberately separates facts from later model judgments.  It
stores daily structure, next-session feedback, promotion rates, sector breadth
and an intraday timeline.  Missing fields stay null and are listed explicitly.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from data_checks import intraday_quality

from hithink_client import CN_TZ, HithinkError, get as hithink_get, pool as hithink_pool
from fetch_hithink import normalize_stock as hithink_stock

DATA_DIR = Path("data")
MARKET_FILE = DATA_DIR / "market_latest.json"
EMOTION_FILE = DATA_DIR / "emotion_latest.json"
HISTORY_FILE = DATA_DIR / "emotion_history.json"
INTRADAY_FILE = DATA_DIR / "intraday_latest.json"
INTRADAY_HISTORY_FILE = DATA_DIR / "intraday_history.json"
HITHINK_FILE = DATA_DIR / "hithink_latest.json"


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def normalize_trading_dates(items: list[dict], end_date: str, target: int) -> list[str]:
    dates = []
    for row in items:
        raw = str(row.get("date") or "").strip()
        if len(raw) == 8 and raw.isdigit():
            raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if parsed <= end_date:
            dates.append(parsed)
    return sorted(set(dates))[-target:]


def requested_trading_dates(end_date: str, target: int = 20) -> tuple[list[str], str]:
    """Resolve exact trading dates instead of probing weekends and holidays."""
    try:
        calendar = hithink_get("/api/a-share/calendar/trading-days")
        dates = normalize_trading_dates(calendar.get("item") or [], end_date, target)
        if len(dates) == target:
            return dates, "同花顺交易日历"
    except HithinkError:
        dates = []

    # The already collected ladder carries a verified 30-trading-day window
    # and is a safe fallback when the calendar endpoint is temporarily down.
    facts = read_json(HITHINK_FILE, {})
    ladder_dates = (((facts.get("limit_up_ladder") or {}).get("window") or {})
                    .get("date_list") or [])
    normalized = normalize_trading_dates(
        [{"date": value} for value in ladder_dates], end_date, target)
    if len(normalized) == target:
        return normalized, "同花顺30日连板天梯日期窗"
    raise RuntimeError(f"Only {len(dates or normalized)} verified trading dates available")


def ladder_summary_by_date() -> dict[str, dict]:
    facts = read_json(HITHINK_FILE, {})
    items = (facts.get("limit_up_ladder") or {}).get("items") or []
    result = {}
    for row in items:
        boards = row.get("boards") or {}
        stocks = [stock for values in boards.values() for stock in (values or [])]
        result[str(row.get("date"))] = {
            "highest_board": max((int(stock.get("board_num") or 0) for stock in stocks), default=1),
            "listed_stocks": len(stocks),
        }
    return result


def collect_hithink_sessions(end_date: str, today: dict,
                              target: int = 20) -> tuple[list[dict], dict]:
    """Build a source-consistent 20-session history with explicit diagnostics."""
    dates, date_source = requested_trading_dates(end_date, target)
    ladder = ladder_summary_by_date()
    errors: dict[str, str] = {}

    def collect(date: str):
        if date == end_date:
            up = today.get("limitUps") or []
            broken_count = today.get("brokenCount")
            down_count = today.get("limitDownCount")
        else:
            try:
                up = [hithink_stock(row) for row in hithink_pool("limitUp", date)]
                if not up:
                    errors[date] = "limit-up-pool returned no rows"
                    return None
                broken_count = len(hithink_pool("broken", date))
                down_count = len(hithink_pool("limitDown", date))
            except Exception as exc:
                errors[date] = f"{type(exc).__name__}: {str(exc)[:180]}"
                return None
        if not up or broken_count is None or down_count is None:
            return None
        heights = Counter(int(r["height"]) for r in up if r.get("height") is not None)
        total = len(up) + broken_count
        ladder_day = ladder.get(date) or {}
        return {"date": date, "limit_up_count": len(up),
                "broken_count": broken_count, "limit_down_count": down_count,
                "seal_rate_pct": round(len(up) / total * 100, 2) if total else None,
                "highest_board": max(heights, default=0),
                "ladder_highest_board": ladder_day.get("highest_board"),
                "ladder_listed_stocks": ladder_day.get("listed_stocks"),
                "height_counts": {str(k): v for k, v in heights.items()},
                "stocks": up}

    # Sequential collection is intentional: 20 dates x 3 pools is modest and
    # avoids losing older sessions to burst rate limiting.
    sessions = [row for date in dates if (row := collect(date))]
    sessions.sort(key=lambda row: row["date"])
    diagnostics = {
        "date_source": date_source,
        "requested_dates": dates,
        "requested_count": len(dates),
        "collected_count": len(sessions),
        "failed_dates": errors,
        "complete": len(sessions) == target and not errors,
    }
    return sessions[-target:], diagnostics


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


def official_theme_structure(market: dict) -> list[dict]:
    """Normalize verified concept/member intersections into cycle inputs."""
    result = []
    for theme in market.get("themes") or []:
        leaders = theme.get("leaders") or []
        heights = Counter(int(row.get("height") or 1) for row in leaders)
        result.append({
            "code": theme.get("code"), "name": theme.get("name"),
            "change_pct": theme.get("change_pct"),
            "turnover_cny": theme.get("turnover_cny"),
            "member_count": theme.get("member_count"),
            "limit_up_count": theme.get("limitUpCount") or 0,
            "first_board_count": heights.get(1, 0),
            "second_board_count": heights.get(2, 0),
            "third_board_count": heights.get(3, 0),
            "high_board_count": sum(v for k, v in heights.items() if k >= 4),
            "max_height": theme.get("maxHeight") or 0,
            "active_days_3": None,
            "leaders": leaders[:4],
            "source": "同花顺概念指数与成分股涨停交集",
        })
    return result


def reason_tokens(reason: str | None) -> list[str]:
    """Split the provider's compound limit-up reason into usable theme tags."""
    return [token.strip() for token in re.split(r"[+＋]", str(reason or ""))
            if token.strip()]


def reason_theme_counts(market: dict) -> Counter:
    return Counter(token for row in market.get("limitUps") or []
                   for token in reason_tokens(row.get("limitReason")))


def reason_theme_structure(market: dict) -> list[dict]:
    """Fallback breadth grouped by the provider's official limit-up reason."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in market.get("limitUps") or []:
        for token in reason_tokens(row.get("limitReason")):
            grouped[token].append(row)
    result = []
    for reason, rows in grouped.items():
        heights = Counter(int(row.get("height") or 1) for row in rows)
        result.append({
            "code": None, "name": reason, "change_pct": None,
            "turnover_cny": None, "member_count": None,
            "limit_up_count": len(rows),
            "first_board_count": heights.get(1, 0),
            "second_board_count": heights.get(2, 0),
            "third_board_count": heights.get(3, 0),
            "high_board_count": sum(v for k, v in heights.items() if k >= 4),
            "max_height": max(heights, default=0), "active_days_3": None,
            "leaders": [{"code": row.get("code"), "name": row.get("name"),
                         "height": row.get("height")} for row in sorted(
                             rows, key=lambda item: -(item.get("height") or 0))[:4]],
            "source": "同花顺涨停原因聚合",
        })
    return result


def merged_theme_structure(market: dict) -> list[dict]:
    official = official_theme_structure(market)
    known = {row.get("name") for row in official}
    fallback = [row for row in reason_theme_structure(market) if row.get("name") not in known]
    return sorted(official + fallback,
                  key=lambda row: (-(row.get("limit_up_count") or 0),
                                   -(row.get("max_height") or 0), row.get("name") or ""))


def snapshot_slot(timestamp: str) -> str | None:
    try:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(CN_TZ)
    except (ValueError, TypeError):
        return None
    minute = stamp.hour * 60 + stamp.minute
    slots = {575: "09:35", 630: "10:30", 690: "11:30", 825: "13:45", 870: "14:30", 900: "15:00"}
    nearest = min(slots, key=lambda value: abs(value - minute))
    return slots[nearest] if abs(nearest - minute) <= 20 else None


def same_source_amount_comparison(history: dict, trade_date: str,
                                  timestamp: str, current_amount,
                                  slot: str | None) -> dict | None:
    """Compare against a prior Tonghuashun snapshot within five minutes."""
    if current_amount is None:
        return None
    days = history.get("days") or {}
    previous_date = next((value for value in reversed(sorted(days)) if value < trade_date), None)
    if not previous_date:
        return None
    try:
        current_stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(CN_TZ)
    except (TypeError, ValueError):
        return None
    current_minute = min(current_stamp.hour * 60 + current_stamp.minute, 900)
    candidates = []
    for row in days.get(previous_date, []):
        if row.get("turnover_cny") is None:
            continue
        if slot and row.get("slot") == slot:
            candidates.append((0, row))
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("time")).replace("Z", "+00:00")).astimezone(CN_TZ)
        except (TypeError, ValueError):
            continue
        delta = abs(min(stamp.hour * 60 + stamp.minute, 900) - current_minute)
        if delta <= 5:
            candidates.append((delta, row))
    if not candidates:
        return None
    previous_amount = min(candidates, key=lambda item: item[0])[1]["turnover_cny"]
    difference = current_amount - previous_amount
    return {
        "status": "ready", "time": slot or f"{current_stamp:%H:%M}",
        "previousTradeDate": previous_date, "current": current_amount,
        "previous": previous_amount, "difference": difference,
        "changePct": round(difference / previous_amount * 100, 2) if previous_amount else None,
        "direction": "增量" if difference > 0 else "缩量" if difference < 0 else "持平",
        "source": "同花顺历史快照同时间比较",
    }


def append_intraday(market: dict, promotion_data: dict | None) -> dict:
    trade_date = market["tradeDate"]
    old = read_json(INTRADAY_FILE, {})
    snapshots = ([row for row in old.get("snapshots", [])
                  if row.get("source") == market.get("source")]
                 if old.get("trade_date") == trade_date
                 and old.get("source") == market.get("source") else [])
    timestamp = market.get("updatedAt") or datetime.now(CN_TZ).isoformat()
    slot = snapshot_slot(timestamp)
    history = read_json(INTRADAY_HISTORY_FILE, {"schema_version": 1, "days": {}})
    current_amount = (market.get("breadth") or {}).get("totalAmount")
    amount_comparison = same_source_amount_comparison(
        history, trade_date, timestamp, current_amount, slot)
    minute = timestamp[:16]
    snapshot = {
        "time": timestamp,
        "source": market.get("source"),
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
        "slot": slot,
        "turnover_cny": current_amount,
        "amount_comparison": amount_comparison,
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

    days = history.setdefault("days", {})
    days[trade_date] = snapshots[-80:]
    for old_date in sorted(days)[:-30]:
        days.pop(old_date, None)
    history["updated_at"] = timestamp
    write_json(INTRADAY_HISTORY_FILE, history)
    return payload


def main() -> None:
    market = read_json(MARKET_FILE, {})
    previous_payload = read_json(EMOTION_FILE, {})
    if not market.get("ok") or not market.get("tradeDate"):
        raise RuntimeError("market_latest.json is unavailable")
    if market.get("source") != "同花顺官方 Financial-API":
        raise RuntimeError("Only Tonghuashun Financial-API market snapshots are accepted")
    hithink = True
    sessions, history_collection = collect_hithink_sessions(market["tradeDate"], market)
    if not sessions:
        raise RuntimeError("No sentiment sessions collected")

    promotion_data = promotion(sessions[-2], sessions[-1]) if len(sessions) >= 2 else None
    previous = sessions[-2] if len(sessions) >= 2 else None
    quote_map = hithink_quotes([row["code"] for row in previous["stocks"]]) if previous else {}
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
    coverage = ((breadth_total / breadth.get("universe") * 100)
                if hithink and breadth.get("universe") else 100 if breadth_total else 0)
    usable_width = breadth.get('complete') is True or (hithink and coverage >= 98)
    width = {
        "available": usable_width,
        "complete": breadth.get('complete') is True,
        "coverage_pct": round(coverage, 2),
        "up": breadth.get("known_up") if hithink and usable_width else breadth.get("up") if usable_width else None,
        "down": breadth.get("known_down") if hithink and usable_width else breadth.get("down") if usable_width else None,
        "flat": breadth.get("known_flat") if hithink and usable_width else breadth.get("flat") if usable_width else None,
        "sample": breadth_total,
    }
    missing = []
    if not width["available"]:
        missing.append("全市场上涨/下跌家数")
    if previous_feedback["sample"] == 0:
        missing.append("昨日涨停次日反馈")
    if hithink:
        if not market.get("themes"):
            missing.append("概念板块成分股涨停扩散与持续性")
        if len(sessions) < 20:
            missing.append("同源20交易日情绪序列")
    intraday = append_intraday(market, promotion_data)
    latest_comparison = next((row.get("amount_comparison") for row in reversed(intraday.get("snapshots") or [])
                              if (row.get("amount_comparison") or {}).get("status") == "ready"), None)
    if latest_comparison:
        market["amountComparison"] = latest_comparison
        write_json(MARKET_FILE, market)
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
    reason_counts = reason_theme_counts(market)
    leaders = [
        {
            "code": row["code"], "name": row["name"],
            "theme": row.get("theme") or max(
                reason_tokens(row.get("limitReason")),
                key=lambda token: (reason_counts[token],
                                   -reason_tokens(row.get("limitReason")).index(token)),
                default=None),
            "theme_source": "concept_membership" if row.get("theme") else "limit_up_reason",
            "height": row["height"], "first_limit": row["firstLimit"],
            "last_limit": row["lastLimit"], "open_count": row["openCount"],
            "seal_amount": row["sealAmount"],
            "max_seal_amount": row.get("maxSealAmount"),
            "seal_retention_pct": (round(row["sealAmount"] / row["maxSealAmount"] * 100, 2)
                                   if row.get("sealAmount") is not None
                                   and row.get("maxSealAmount") else None),
            "amount": row["amount"],
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
        "themes": merged_theme_structure(market) if hithink else theme_structure(sessions),
        "intraday": {
            "snapshot_count": intraday["data_quality"]["snapshot_count"],
            "timeline_ready": intraday["data_quality"]["timeline_ready"],
        },
        "data_quality": {
            "daily_sessions": len(sessions),
            "history_collection": history_collection,
            "complete": not missing,
            "missing": missing,
            "confidence": "high" if not missing else ("medium" if len(missing) <= 2 else "low"),
        },
    }
    history = {
        "schema_version": 1,
        "ok": True,
        "updated_at": market.get("updatedAt"),
        "collection": history_collection,
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
