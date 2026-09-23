#!/usr/bin/env python3
"""Merge current facts and cycle judgments into the durable workbench bundle."""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path("data")


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def save(name: str, payload: dict) -> None:
    path = DATA / name
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def index_map(market: dict) -> dict:
    return {row.get("name"): row for row in market.get("indices") or []}


def build_daily(market: dict, emotion: dict, cycle: dict) -> dict:
    indices = index_map(market)
    sh, sz = indices.get("上证指数", {}), indices.get("深证成指", {})
    cy, kc = indices.get("创业板指", {}), indices.get("科创50", {})
    ecology = emotion.get("market_ecology") or {}
    breadth = ecology.get("market_width") or {}
    mainline = (cycle.get("medium_cycle") or {}).get("mainline") or {}
    return {
        "date": market.get("tradeDate"),
        "shanghai_close": sh.get("price"), "shanghai_pct": sh.get("change"),
        "shenzhen_close": sz.get("price"), "shenzhen_pct": sz.get("change"),
        "chinext_close": cy.get("price"), "chinext_pct": cy.get("change"),
        "star50_close": kc.get("price"), "star50_pct": kc.get("change"),
        "turnover_cny": ((market.get("breadth") or {}).get("totalAmount")
                         if market.get("source") == "同花顺官方 Financial-API"
                         else (sh.get("amount") or 0) + (sz.get("amount") or 0) or None),
        "up_count": breadth.get("up") if breadth.get("available") else None,
        "down_count": breadth.get("down") if breadth.get("available") else None,
        "flat_count": breadth.get("flat") if breadth.get("available") else None,
        "limit_up_count": ecology.get("limit_up_count"),
        "limit_down_count": ecology.get("limit_down_count"),
        "failed_limit_count": ecology.get("broken_count"),
        "broken_rate_pct": ecology.get("broken_rate_pct"),
        "seal_rate_pct": ecology.get("seal_rate_pct"),
        "highest_board": ecology.get("highest_board"),
        "big_cycle": (cycle.get("large_cycle") or {}).get("stage"),
        "mid_cycle": mainline.get("stage"),
        "small_cycle": (cycle.get("small_cycle") or {}).get("stage"),
        "primary_theme": mainline.get("theme"),
        "structure_label": (cycle.get("dashboard") or {}).get("structure_label"),
        "data_quality": "auto_collected_partial" if (cycle.get("data_quality") or {}).get("missing") else "auto_collected_complete",
    }


def build_ladder(market: dict, details: dict | None = None) -> dict:
    grouped: dict[str, list[dict]] = {}
    details = details or {}
    auction = {r.get("ticker"): r for r in (details.get("auction") or {}).get("items") or []}
    anomaly = {r.get("thscode"): r for r in details.get("anomaly_analysis") or []}
    for row in market.get("limitUps") or []:
        height = str(row.get("height") or 1)
        grouped.setdefault(height, []).append({
            "code": row.get("code"), "name": row.get("name"), "theme": row.get("theme"),
            "height": row.get("height"), "first_limit": row.get("firstLimit"),
            "last_limit": row.get("lastLimit"), "open_count": row.get("openCount"),
            "amount": row.get("amount"), "seal_amount": row.get("sealAmount"),
            "limit_reason": row.get("limitReason"), "price": row.get("price"),
            "auction": auction.get(row.get("code")),
            "anomaly_analysis": anomaly.get(row.get("thscode")),
            "data_source": market.get("source"),
        })
    return {key: grouped[key] for key in sorted(grouped, key=int, reverse=True)}


def main() -> None:
    workbench = load("workbench_latest.json")
    market, emotion = load("market_latest.json"), load("emotion_latest.json")
    cycle, intraday = load("cycle_analysis_latest.json"), load("intraday_latest.json")
    details = load("hithink_latest.json") if market.get("source") == "同花顺官方 Financial-API" else {}
    date = market["tradeDate"]

    workbench.setdefault("meta", {})["generated_at"] = cycle.get("generated_at")
    workbench["meta"]["market_trade_date"] = date
    dates = list(workbench["meta"].get("data_range") or [])
    workbench["meta"]["data_range"] = [min(dates + [date]), max(dates + [date])] if dates else [date, date]
    notes = workbench["meta"].setdefault("notes", [])
    note = "三层周期基于情绪结构、龙头生态和连续性，不使用指数均线。"
    if note not in notes:
        notes.append(note)

    daily = build_daily(market, emotion, cycle)
    rows = [row for row in workbench.get("market_daily", []) if row.get("date") != date]
    workbench["market_daily"] = sorted(rows + [daily], key=lambda row: row.get("date") or "")

    mainline = (cycle.get("medium_cycle") or {}).get("mainline")
    workbench["market_cycle"] = {
        "large_cycle": cycle.get("large_cycle"),
        "medium_cycle": {"mainline": mainline, "themes": (cycle.get("medium_cycle") or {}).get("themes", [])},
        "small_cycle": cycle.get("small_cycle"),
        "leader_ecology_matrix": cycle.get("leader_ecology_matrix"),
        "dashboard": cycle.get("dashboard"),
        "method": "评分 + 结构 + 连续性；严禁用均线替代情绪结构。",
    }
    timeline = workbench.setdefault("cycle_timeline", {})
    big = [row for row in timeline.get("big_cycle", []) if row.get("date") != date]
    big.append({"date": date, "state": (cycle.get("large_cycle") or {}).get("stage"), "score": (cycle.get("large_cycle") or {}).get("score")})
    timeline["big_cycle"] = sorted(big, key=lambda row: row["date"])
    mid = [row for row in timeline.get("mid_cycle", []) if row.get("date") != date]
    mid.append({"date": date, "theme": (mainline or {}).get("theme"), "state": (mainline or {}).get("stage"), "score": (mainline or {}).get("score")})
    timeline["mid_cycle"] = sorted(mid, key=lambda row: row["date"])
    timeline["intraday"] = intraday.get("snapshots") or []

    workbench.setdefault("ladder", {})[date] = build_ladder(market, details)
    if details:
        workbench["hithink_facts"] = details
    workbench["sector_strength"] = {
        "date": date,
        "method": "龙头强度30% + 板块宽度25% + 梯队完整度25% + 资金持续性20%",
        "sectors": (cycle.get("medium_cycle") or {}).get("themes", []),
        "official_sector_facts": details.get("sectors") if details else None,
    }

    old_quality = workbench.get("data_quality") or {}
    missing = list(dict.fromkeys((old_quality.get("missing_fields") or []) + (cycle.get("data_quality") or {}).get("missing", [])))
    missing = [item for item in missing if item not in ("完整20日与60日周期序列", "9月22日竞价与盘中承接数据")]
    if not (cycle.get("small_cycle") or {}).get("timeline_ready"):
        missing.append("9月22日完整盘中情绪时间轴")
    workbench["data_quality"] = {
        **old_quality,
        "as_of_date": date,
        "market_data": True,
        "limit_up_data": True,
        "intraday_data": bool((cycle.get("small_cycle") or {}).get("timeline_ready")),
        "market_width_data": not ("市场上涨宽度" in missing),
        "cycle_analysis_data": True,
        "missing_fields": list(dict.fromkeys(missing)),
        "analysis_allowed": bool((cycle.get("data_quality") or {}).get("analysis_allowed")),
        "intraday_trigger_validation_allowed": False,
        "analysis_scope": "允许盘后结构与三层周期分析；缺失市场宽度或完整盘中时间轴时降低置信度，不补造。",
    }
    save("workbench_latest.json", workbench)
    print(json.dumps({"date": date, "daily_rows": len(workbench["market_daily"]), "ladder_stocks": sum(len(v) for v in workbench["ladder"][date].values()), "missing": workbench["data_quality"]["missing_fields"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
