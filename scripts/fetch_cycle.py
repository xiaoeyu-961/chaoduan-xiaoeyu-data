#!/usr/bin/env python3
"""Calculate 60/20/5-session A-share market cycles from public market data."""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CN_TZ = timezone(timedelta(hours=8))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
}
INDEXES = {
    "shanghai": ("1.000001", "上证指数"),
    "shenzhen": ("0.399001", "深证成指"),
    "chinext": ("0.399006", "创业板指"),
}
KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&klt=101&fqt=1&lmt=90"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
)
POOL_URLS = {
    "limitUp": "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
    "broken": "https://push2ex.eastmoney.com/getTopicZBPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
    "limitDown": "https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt:asc",
}


def number(value, default=0.0):
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def get_json(url: str, attempts: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=UA, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(str(last))


def fetch_index(secid: str, name: str) -> dict:
    body = get_json(KLINE_URL.format(secid=secid))
    rows = []
    for raw in (body.get("data") or {}).get("klines") or []:
        parts = raw.split(",")
        if len(parts) < 11:
            continue
        rows.append({
            "date": parts[0],
            "open": number(parts[1]),
            "close": number(parts[2]),
            "high": number(parts[3]),
            "low": number(parts[4]),
            "volume": number(parts[5]),
            "amount": number(parts[6]),
            "amplitude_pct": number(parts[7]),
            "change_pct": number(parts[8]),
            "turnover_pct": number(parts[10]),
        })
    if len(rows) < 60:
        raise RuntimeError(f"{name}: only {len(rows)} sessions")
    return {"name": name, "rows": rows[-60:]}


def mean(values):
    values = [number(x) for x in values if x is not None]
    return statistics.fmean(values) if values else 0.0


def pct_change(last, first):
    return round((last / first - 1) * 100, 2) if first else 0.0


def index_metrics(series: dict) -> dict:
    rows = series["rows"]
    closes = [x["close"] for x in rows]
    amounts = [x["amount"] for x in rows]
    last = rows[-1]
    return {
        "name": series["name"],
        "as_of": last["date"],
        "close": last["close"],
        "return_5d_pct": pct_change(closes[-1], closes[-6]),
        "return_20d_pct": pct_change(closes[-1], closes[-21]),
        "return_60d_pct": pct_change(closes[-1], closes[0]),
        "ma5": round(mean(closes[-5:]), 2),
        "ma10": round(mean(closes[-10:]), 2),
        "ma20": round(mean(closes[-20:]), 2),
        "ma60": round(mean(closes), 2),
        "amount_avg_5d": round(mean(amounts[-5:]), 2),
        "amount_avg_20d": round(mean(amounts[-20:]), 2),
        "amount_avg_60d": round(mean(amounts), 2),
        "above_ma5": closes[-1] >= mean(closes[-5:]),
        "above_ma20": closes[-1] >= mean(closes[-20:]),
        "above_ma60": closes[-1] >= mean(closes),
    }


def fetch_sentiment_history(limit: int = 20) -> list[dict]:
    result = []
    cursor = datetime.now(CN_TZ)
    for offset in range(45):
        if len(result) >= limit:
            break
        day = (cursor - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            up = (get_json(f"{POOL_URLS['limitUp']}&date={day}").get("data") or {}).get("pool") or []
        except Exception:
            continue
        if not up:
            continue
        try:
            broken = (get_json(f"{POOL_URLS['broken']}&date={day}").get("data") or {}).get("pool") or []
        except Exception:
            broken = []
        try:
            down = (get_json(f"{POOL_URLS['limitDown']}&date={day}").get("data") or {}).get("pool") or []
        except Exception:
            down = []
        highest = max((int(number(x.get("lbc"), 1)) for x in up), default=0)
        denominator = len(up) + len(broken)
        result.append({
            "date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "limit_up_count": len(up),
            "broken_count": len(broken),
            "limit_down_count": len(down),
            "highest_board": highest,
            "seal_rate_pct": round(len(up) / denominator * 100, 2) if denominator else 0,
        })
    return list(reversed(result))


def classify_long(sh: dict, sz: dict, amount: dict) -> tuple[str, str, int]:
    avg60_ret = mean([sh["return_60d_pct"], sz["return_60d_pct"]])
    avg20_ret = mean([sh["return_20d_pct"], sz["return_20d_pct"]])
    above60 = int(sh["above_ma60"]) + int(sz["above_ma60"])
    amount_ratio = amount["ratio_5d_to_60d"]
    if above60 == 2 and avg60_ret >= 8 and avg20_ret > 0:
        return "上升周期", "指数位于60日均线之上，中长期收益为正", 82
    if above60 == 2 and avg60_ret > 0:
        return "震荡上行", "指数保持60日均线上方，但斜率与强度未达主升", 72
    if avg60_ret > -4 and avg60_ret < 4:
        return "区间震荡", "60日累计涨跌有限，趋势性不足", 55
    if above60 >= 1 and avg20_ret < 0:
        return "上升后的调整", "长期结构尚未完全破坏，中期动能转弱", 48
    return "下降周期", "指数位于60日均线下方且中长期回报偏弱", 32


def classify_medium(sh: dict, sz: dict, amount: dict, sentiment: list[dict]) -> tuple[str, str, int]:
    avg20_ret = mean([sh["return_20d_pct"], sz["return_20d_pct"]])
    above20 = int(sh["above_ma20"]) + int(sz["above_ma20"])
    recent = sentiment[-5:]
    height = mean([x["highest_board"] for x in recent])
    seal = mean([x["seal_rate_pct"] for x in recent])
    if above20 == 2 and avg20_ret >= 5 and height >= 4:
        return "主升扩张", "指数20日趋势向上，高标与封板结构同步活跃", 82
    if above20 == 2 and avg20_ret > 1:
        return "修复转强", "指数重回20日均线上方，情绪结构处于增强阶段", 70
    if avg20_ret > -2 and avg20_ret <= 2:
        return "震荡分化", "20日指数方向有限，题材轮动主导", 56
    if above20 >= 1 and seal >= 65:
        return "分歧整理", "指数动能回落，但短线封板质量尚未失守", 50
    return "退潮调整", "指数与短线情绪同时转弱", 35


def classify_short(sh: dict, sz: dict, amount: dict, sentiment: list[dict]) -> tuple[str, str, int]:
    avg5_ret = mean([sh["return_5d_pct"], sz["return_5d_pct"]])
    recent = sentiment[-5:]
    up_avg = mean([x["limit_up_count"] for x in recent])
    height = mean([x["highest_board"] for x in recent])
    seal = mean([x["seal_rate_pct"] for x in recent])
    current = recent[-1] if recent else {}
    score = 50
    score += 12 if avg5_ret > 1 else (-12 if avg5_ret < -1 else 0)
    score += 10 if amount["ratio_5d_to_20d"] >= 1 else -5
    score += 10 if seal >= 70 else (-10 if seal < 55 else 0)
    score += 10 if height >= 4 else (-8 if height < 3 else 0)
    score = max(0, min(100, round(score)))
    if score >= 75:
        state = "强修复 / 扩张"
    elif score >= 60:
        state = "偏强轮动"
    elif score >= 45:
        state = "分歧震荡"
    elif score >= 30:
        state = "退潮"
    else:
        state = "冰点"
    reason = f"近5日指数均值{avg5_ret:+.2f}%，涨停均值{up_avg:.1f}家，最高板均值{height:.1f}，封板率均值{seal:.1f}%"
    return state, reason, score


def main() -> None:
    series = {key: fetch_index(secid, name) for key, (secid, name) in INDEXES.items()}
    metrics = {key: index_metrics(value) for key, value in series.items()}
    sh, sz = metrics["shanghai"], metrics["shenzhen"]
    total_amounts = [
        series["shanghai"]["rows"][i]["amount"] + series["shenzhen"]["rows"][i]["amount"]
        for i in range(60)
    ]
    amount = {
        "avg_5d": round(mean(total_amounts[-5:]), 2),
        "avg_20d": round(mean(total_amounts[-20:]), 2),
        "avg_60d": round(mean(total_amounts), 2),
    }
    amount["ratio_5d_to_20d"] = round(amount["avg_5d"] / amount["avg_20d"], 3) if amount["avg_20d"] else 0
    amount["ratio_5d_to_60d"] = round(amount["avg_5d"] / amount["avg_60d"], 3) if amount["avg_60d"] else 0

    sentiment = fetch_sentiment_history(20)
    long_state, long_reason, long_score = classify_long(sh, sz, amount)
    medium_state, medium_reason, medium_score = classify_medium(sh, sz, amount, sentiment)
    short_state, short_reason, short_score = classify_short(sh, sz, amount, sentiment)
    total_score = round(long_score * 0.35 + medium_score * 0.4 + short_score * 0.25)

    payload = {
        "schema_version": 1,
        "ok": True,
        "source": "东方财富公开行情 · GitHub Actions计算",
        "calculated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_date": sh["as_of"],
        "method": {
            "long_cycle": "60交易日指数趋势与成交额中枢，权重35%",
            "medium_cycle": "20交易日指数趋势与短线情绪，权重40%",
            "short_cycle": "5交易日指数、量能、涨停高度与封板率，权重25%",
        },
        "long_cycle": {"period": "60交易日", "state": long_state, "trend": long_reason, "score": long_score},
        "medium_cycle": {"period": "20交易日", "state": medium_state, "trend": medium_reason, "score": medium_score},
        "short_cycle": {"period": "5交易日", "state": short_state, "trend": short_reason, "score": short_score},
        "cycle_gate": {
            "score": total_score,
            "state": "开启" if total_score >= 70 else ("谨慎" if total_score >= 45 else "关闭"),
            "note": "周期总开关只决定策略环境，不代替个股竞价与承接验收。",
        },
        "indices": metrics,
        "turnover": amount,
        "sentiment_history": sentiment,
        "data_quality": {
            "index_sessions": 60,
            "sentiment_sessions": len(sentiment),
            "breadth_history_available": False,
            "complete": len(sentiment) >= 15,
            "missing": ["历史每日上涨/下跌家数"],
        },
    }
    target = Path("data/cycle_latest.json")
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    print(json.dumps({
        "as_of_date": payload["as_of_date"],
        "long_cycle": long_state,
        "medium_cycle": medium_state,
        "short_cycle": short_state,
        "cycle_gate": total_score,
        "sentiment_sessions": len(sentiment),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
