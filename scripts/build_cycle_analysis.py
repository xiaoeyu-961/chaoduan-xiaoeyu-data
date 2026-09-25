#!/usr/bin/env python3
"""Build explainable short-term cycle judgments from collected facts."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from data_checks import intraday_quality

DATA = Path("data")
EMOTION = DATA / "emotion_latest.json"
HISTORY = DATA / "emotion_history.json"
INTRADAY = DATA / "intraday_latest.json"
OUTPUT = DATA / "cycle_analysis_latest.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def clamp(value: float) -> float:
    return max(0, min(100, value))


def rising(value, bad: float, good: float):
    return None if value is None else clamp((value - bad) / (good - bad) * 100)


def falling(value, good: float, bad: float):
    return None if value is None else clamp((bad - value) / (bad - good) * 100)


def weighted(items: list[dict]) -> dict:
    available = [item for item in items if item["normalized"] is not None]
    used = sum(item["weight"] for item in available)
    total = sum(item["weight"] for item in items)
    score = round(sum(item["normalized"] * item["weight"] for item in available) / used) if used else None
    components = []
    for item in items:
        components.append({
            **item,
            "normalized": None if item["normalized"] is None else round(item["normalized"], 2),
            "contribution": None if item["normalized"] is None or not used else round(item["normalized"] * item["weight"] / used, 2),
        })
    return {
        "score": score,
        "coverage_pct": round(used / total * 100, 1) if total else 0,
        "components": components,
        "missing_metrics": [item["metric"] for item in items if item["normalized"] is None],
    }


def series_trend(values: list) -> str:
    clean = [float(value) for value in values if value is not None]
    if len(clean) < 3:
        return "unknown"
    recent = statistics.fmean(clean[-2:])
    prior = statistics.fmean(clean[-4:-2]) if len(clean) >= 4 else clean[0]
    band = max(2.0, abs(prior) * 0.08)
    return "up" if recent > prior + band else "down" if recent < prior - band else "flat"


def leader_strength(emotion: dict) -> dict:
    leaders = emotion.get("leader_candidates") or []
    leader = leaders[0] if leaders else None
    if not leader:
        return {"score": None, "coverage_pct": 0, "components": [], "missing_metrics": ["龙头事实"], "leader": None, "label": "数据缺失"}
    amount = leader.get("amount") or 0
    seal_ratio = leader.get("seal_amount") / amount * 100 if leader.get("seal_amount") is not None and amount else None
    first = str(leader.get("first_limit") or "")
    compact_first = first.replace(":", "")
    first_minutes = (int(compact_first[:2]) * 60 + int(compact_first[2:4])
                     if len(compact_first) == 4 and compact_first.isdigit() else None)
    theme_count = next((t.get("limit_up_count") for t in emotion.get("themes", []) if t.get("name") == leader.get("theme")), None)
    open_count = leader.get("open_count")
    retention = leader.get("seal_retention_pct")
    if open_count is not None:
        stability_value, stability_score, stability_basis = open_count, falling(open_count, 0, 5), "开板次数"
    elif retention is not None:
        stability_value, stability_score, stability_basis = retention, rising(retention, 20, 100), "当前封单额/峰值封单额"
    elif seal_ratio is not None:
        # Historical limit-up responses may expose max_seal_money as null.
        # The stock is still in the closing limit-up pool, so use a disclosed
        # proxy rather than pretending an unavailable open-count is zero.
        stability_value = round(seal_ratio, 2)
        stability_score = 40 + rising(seal_ratio, 1, 50) * .6
        stability_basis = "收盘仍封住+封单额/成交额代理"
    else:
        stability_value, stability_score, stability_basis = None, None, "数据缺失"
    result = weighted([
        {"metric": "龙头高度", "value": leader.get("height"), "weight": 30, "normalized": rising(leader.get("height"), 2, 7)},
        {"metric": "封板稳定性", "value": stability_value, "weight": 25,
         "normalized": stability_score,
         "basis": stability_basis},
        {"metric": "首次封板主动性", "value": leader.get("first_limit"), "weight": 20, "normalized": falling(first_minutes, 570, 870)},
        {"metric": "封单/成交额", "value": None if seal_ratio is None else round(seal_ratio, 2), "weight": 15, "normalized": rising(seal_ratio, 1, 50)},
        {"metric": "龙头所在题材涨停数", "value": theme_count, "weight": 10, "normalized": rising(theme_count, 1, 8)},
    ])
    result.update({"leader": {key: leader.get(key) for key in ("code", "name", "theme", "height")},
                   "label": "数据不足" if result["score"] is None or result["coverage_pct"] < 65
                   else "强" if result["score"] >= 65 else "弱"})
    return result


def market_ecology(emotion: dict) -> dict:
    ecology = emotion.get("market_ecology") or {}
    feedback = ecology.get("previous_limit_up_feedback") or {}
    middle = ecology.get("middle_position_feedback") or {}
    width = ecology.get("market_width") or {}
    width_value = None
    if width.get("available") and (width.get("up") or 0) + (width.get("down") or 0):
        width_value = width["up"] / (width["up"] + width["down"]) * 100
    result = weighted([
        {"metric": "涨停数量", "value": ecology.get("limit_up_count"), "weight": 16, "normalized": rising(ecology.get("limit_up_count"), 20, 100)},
        {"metric": "炸板率", "value": ecology.get("broken_rate_pct"), "weight": 16, "normalized": falling(ecology.get("broken_rate_pct"), 10, 50)},
        {"metric": "连板晋级率", "value": (ecology.get("promotion") or {}).get("rate_pct"), "weight": 18, "normalized": rising((ecology.get("promotion") or {}).get("rate_pct"), 10, 50)},
        {"metric": "昨日涨停平均反馈", "value": feedback.get("avg_change_pct"), "weight": 14, "normalized": rising(feedback.get("avg_change_pct"), -4, 6)},
        {"metric": "中位股平均反馈", "value": middle.get("avg_change_pct"), "weight": 12, "normalized": rising(middle.get("avg_change_pct"), -5, 5)},
        {"metric": "中位股大亏比例", "value": middle.get("loss_below_minus_5_rate_pct"), "weight": 10, "normalized": falling(middle.get("loss_below_minus_5_rate_pct"), 3, 35)},
        {"metric": "跌停数量", "value": ecology.get("limit_down_count"), "weight": 8, "normalized": falling(ecology.get("limit_down_count"), 0, 20)},
        {"metric": "市场上涨宽度", "value": None if width_value is None else round(width_value, 2), "weight": 6, "normalized": rising(width_value, 25, 75)},
    ])
    result["label"] = ("数据不足" if result["score"] is None or result["coverage_pct"] < 65
                       else "强" if result["score"] >= 65 else "弱")
    return result


def matrix_label(leader: dict, ecology: dict) -> str:
    if leader.get("label") not in ("强", "弱") or ecology.get("label") not in ("强", "弱"):
        return "结构数据不足 / 暂无法判断"
    strong = (leader.get("label") == "强", ecology.get("label") == "强")
    return {(True, True): "龙头强 / 生态强：健康主升", (True, False): "龙头强 / 生态弱：龙头抱团 / 生态分歧", (False, True): "龙头弱 / 生态强：高低切 / 新旧切换", (False, False): "龙头弱 / 生态弱：退潮风险"}[strong]


def large_cycle(emotion: dict, history: dict, leader: dict, ecology: dict) -> dict:
    sessions = history.get("sessions") or []
    current = sessions[-1] if sessions else {}
    lu_trend = series_trend([row.get("limit_up_count") for row in sessions])
    seal_trend = series_trend([row.get("seal_rate_pct") for row in sessions])
    height_trend = series_trend([row.get("highest_board") for row in sessions])
    score = round(leader["score"] * .28 + ecology["score"] * .72) if leader.get("score") is not None and ecology.get("score") is not None else None
    e = emotion.get("market_ecology") or {}
    promotion = (e.get("promotion") or {}).get("rate_pct")
    broken = e.get("broken_rate_pct")
    feedback = (e.get("previous_limit_up_feedback") or {}).get("avg_change_pct")
    if score is None or leader.get("label") == "数据不足" or ecology.get("label") == "数据不足" or len(sessions) < 3:
        stage = "数据缺失 / 暂无法判断"
    elif leader["label"] == "强" and ecology["label"] == "弱":
        stage = "分歧"
    elif ecology["score"] < 35 and lu_trend == "down":
        stage = "退潮"
    elif ecology["score"] < 42:
        stage = "混沌"
    elif lu_trend == "up" and height_trend == "up" and (promotion or 0) >= 25:
        stage = "发酵"
    elif ecology["score"] >= 72 and seal_trend != "down" and (feedback or -99) > 1:
        stage = "主升"
    elif ecology["score"] >= 60:
        stage = "确认"
    else:
        stage = "启动"
    supports, pressures = [], []
    checks = [
        (current.get("highest_board", 0) >= 5, f"最高板{current.get('highest_board')}板", "最高板高度不足"),
        ((promotion or 0) >= 30, f"晋级率{promotion}%", f"晋级率仅{promotion}%"),
        ((broken or 100) <= 25, f"炸板率{broken}%可控", f"炸板率{broken}%偏高"),
        ((feedback or -99) > 0, f"昨日涨停平均反馈{feedback}%为正", f"昨日涨停平均反馈{feedback}%偏弱"),
        (lu_trend == "up", "近端涨停数量改善", "近端涨停数量未形成上升趋势"),
    ]
    for ok, positive, negative in checks:
        (supports if ok else pressures).append(positive if ok else negative)
    coverage = min(leader.get("coverage_pct", 0), ecology.get("coverage_pct", 0))
    critical_missing = bool(leader.get("missing_metrics") or ecology.get("missing_metrics"))
    confidence = "high" if coverage >= 85 and len(sessions) >= 10 and not critical_missing else "medium" if coverage >= 65 and len(sessions) >= 5 else "low"
    return {"stage": stage, "score": score, "trend": "↑" if lu_trend == "up" and height_trend != "down" else "↓" if lu_trend == "down" else "→", "method": "评分 + 结构 + 连续性；不使用均线", "continuity": {"limit_up": lu_trend, "seal_rate": seal_trend, "highest_board": height_trend}, "supporting_factors": supports, "pressure_factors": pressures, "confidence": confidence, "conclusion": f"{stage}；{matrix_label(leader, ecology)}"}


def theme_cycles(emotion: dict) -> list[dict]:
    output = []
    for theme in emotion.get("themes") or []:
        width = rising(theme.get("limit_up_count"), 1, 10)
        leader = rising(theme.get("max_height"), 1, 7)
        levels = sum((theme.get(key) or 0) > 0 for key in ("first_board_count", "second_board_count", "third_board_count", "high_board_count"))
        ladder = levels / 4 * 100
        persistence = rising(theme.get("active_days_3"), 1, 3)
        explanation = weighted([
            {"metric": "龙头强度", "value": theme.get("max_height"), "weight": 30, "normalized": leader},
            {"metric": "板块宽度", "value": theme.get("limit_up_count"), "weight": 25, "normalized": width},
            {"metric": "梯队完整度", "value": levels, "weight": 25, "normalized": ladder},
            {"metric": "资金持续性", "value": theme.get("active_days_3"), "weight": 20, "normalized": persistence},
        ])
        score = explanation["score"] or 0
        if theme.get("active_days_3") == 1: stage = "萌芽"
        elif score >= 80 and width >= 65 and ladder >= 75: stage = "一致"
        elif leader >= 65 and width < 45: stage = "发酵偏分歧"
        elif score >= 62: stage = "扩散"
        elif score >= 45: stage = "发酵"
        else: stage = "萌芽"
        output.append({"theme": theme.get("name"), "stage": stage, "score": score, "trend": "↑" if (theme.get("active_days_3") or 0) >= 2 else "→", "subscores": {"leader_strength": round(leader or 0), "sector_width": round(width or 0), "ladder_completeness": round(ladder), "capital_persistence": round(persistence or 0)}, "facts": theme, "explanation": explanation})
    return sorted(output, key=lambda row: (-row["score"], row["theme"]))


def small_cycle(intraday: dict) -> dict:
    snapshots, quality = intraday_quality(intraday.get("snapshots") or [], intraday.get("trade_date"))
    ready = quality['timeline_ready']
    if not ready:
        return {"stage": "数据不足 / 暂无法判断", "score": None, "trend": "—", "timeline_ready": False, "snapshot_count": len(snapshots), "required_snapshot_count": 6, "timeline": [], "note": "不会用收盘单点反推全天路径。"}
    timeline, previous = [], None
    for row in snapshots:
        if previous is None: node = "观察起点"
        else:
            delta_up = (row.get("limit_up_count") or 0) - (previous.get("limit_up_count") or 0)
            delta_broken = (row.get("broken_rate_pct") or 0) - (previous.get("broken_rate_pct") or 0)
            if delta_up >= 3 and delta_broken <= 0: node = "修复" if (row.get("broken_rate_pct") or 0) > 20 else "一致"
            elif delta_up <= -3 or delta_broken >= 5: node = "分歧" if (row.get("limit_up_count") or 0) >= 30 else "转弱"
            elif delta_up > 0 and delta_broken < 0: node = "回流"
            else: node = "震荡"
        facts = dict(row)
        comparison = facts.get("amount_comparison") or {}
        if comparison.get("source") != "同花顺历史快照同时间比较":
            facts["amount_comparison"] = None
        timeline.append({"time": row.get("time"), "node": node, "facts": facts})
        previous = row
    return {"stage": timeline[-1]["node"], "score": None, "trend": "→", "timeline_ready": True, "snapshot_count": len(snapshots), "timeline": timeline, "note": "日内节点不直接改写大周期。"}


def factor_pack(emotion: dict, history: dict, leader: dict,
                ecology: dict, themes: list[dict]) -> dict:
    """Build the seven factual dimensions used by the state machine."""
    raw = emotion.get("market_ecology") or {}
    promotion = raw.get("promotion") or {}
    by_height = promotion.get("by_height") or {}
    sessions = history.get("sessions") or []
    current = sessions[-1] if sessions else {}
    counts = current.get("height_counts") or {}
    ladder_layers = sum(bool(sum(int(v) for k, v in counts.items()
                                 if (int(k) >= 5 if level == 5 else int(k) == level)))
                        for level in (2, 3, 4, 5))
    def tier_rate(height):
        if height < 4:
            return (by_height.get(str(height)) or {}).get("rate_pct")
        eligible = promoted = 0
        for key, value in by_height.items():
            if int(key) >= 4:
                eligible += value.get("eligible") or 0
                promoted += value.get("promoted") or 0
        return promoted / eligible * 100 if eligible else None
    relay = weighted([
        {"metric": "1进2晋级率", "value": tier_rate(1), "weight": 15, "normalized": rising(tier_rate(1), 10, 55)},
        {"metric": "2进3晋级率", "value": tier_rate(2), "weight": 20, "normalized": rising(tier_rate(2), 10, 55)},
        {"metric": "3进4晋级率", "value": tier_rate(3), "weight": 20, "normalized": rising(tier_rate(3), 10, 55)},
        {"metric": "4板以上晋级率", "value": tier_rate(4), "weight": 15, "normalized": rising(tier_rate(4), 10, 55)},
        {"metric": "梯队完整度", "value": ladder_layers, "weight": 15, "normalized": ladder_layers / 4 * 100},
        {"metric": "连板数量趋势", "value": series_trend([sum(int(v) for k, v in (row.get("height_counts") or {}).items() if int(k) >= 2) for row in sessions]), "weight": 10, "normalized": {"up": 80, "flat": 50, "down": 20}.get(series_trend([sum(int(v) for k, v in (row.get("height_counts") or {}).items() if int(k) >= 2) for row in sessions]))},
        {"metric": "最高板趋势", "value": series_trend([row.get("highest_board") for row in sessions]), "weight": 5, "normalized": {"up": 80, "flat": 50, "down": 20}.get(series_trend([row.get("highest_board") for row in sessions]))},
    ])
    previous = raw.get("previous_limit_up_feedback") or {}
    middle = raw.get("middle_position_feedback") or {}
    high = raw.get("high_position_feedback") or {}
    profit = weighted([
        {"metric": "昨日涨停中位收益", "value": previous.get("median_change_pct"), "weight": 20, "normalized": rising(previous.get("median_change_pct"), -4, 5)},
        {"metric": "昨日涨停红盘率", "value": previous.get("positive_rate_pct"), "weight": 15, "normalized": rising(previous.get("positive_rate_pct"), 30, 70)},
        {"metric": "昨日涨停平均收益", "value": previous.get("avg_change_pct"), "weight": 10, "normalized": rising(previous.get("avg_change_pct"), -4, 5)},
        {"metric": "中位股中位收益", "value": middle.get("median_change_pct"), "weight": 20, "normalized": rising(middle.get("median_change_pct"), -5, 5)},
        {"metric": "高位核心反馈", "value": high.get("avg_change_pct"), "weight": 15, "normalized": rising(high.get("avg_change_pct"), -5, 7)},
        {"metric": "炸板股次日修复", "value": None, "weight": 10, "normalized": None},
        {"metric": "封板质量", "value": raw.get("seal_rate_pct"), "weight": 10, "normalized": rising(raw.get("seal_rate_pct"), 50, 90)},
    ])
    loss = weighted([
        {"metric": "昨日涨停大亏率", "value": previous.get("loss_below_minus_5_rate_pct"), "weight": 20, "normalized": rising(previous.get("loss_below_minus_5_rate_pct"), 3, 35)},
        {"metric": "中位股大亏率", "value": middle.get("loss_below_minus_5_rate_pct"), "weight": 25, "normalized": rising(middle.get("loss_below_minus_5_rate_pct"), 3, 40)},
        {"metric": "高位核心负反馈", "value": high.get("loss_below_minus_5_rate_pct"), "weight": 20, "normalized": rising(high.get("loss_below_minus_5_rate_pct"), 0, 35)},
        {"metric": "跌停数量", "value": raw.get("limit_down_count"), "weight": 15, "normalized": rising(raw.get("limit_down_count"), 0, 20)},
        {"metric": "天地板及严重亏损", "value": None, "weight": 10, "normalized": None},
        {"metric": "炸板股次日负反馈", "value": None, "weight": 10, "normalized": None},
    ])
    width = raw.get("market_width") or {}
    width_pct = (width.get("up") / (width.get("up") + width.get("down")) * 100
                 if width.get("available") and (width.get("up") or 0) + (width.get("down") or 0) else None)
    mainline = themes[0] if themes else None
    seal = weighted([
        {"metric": "封板率", "value": raw.get("seal_rate_pct"), "weight": 60, "normalized": rising(raw.get("seal_rate_pct"), 50, 90)},
        {"metric": "炸板率", "value": raw.get("broken_rate_pct"), "weight": 40, "normalized": falling(raw.get("broken_rate_pct"), 10, 50)},
    ])
    packs = {
        "R": relay, "P": profit, "L": loss,
        "C": leader,
        "M": ({"score": mainline.get("score"), "coverage_pct": (mainline.get("explanation") or {}).get("coverage_pct", 0), "components": (mainline.get("explanation") or {}).get("components", []), "missing_metrics": (mainline.get("explanation") or {}).get("missing_metrics", [])} if mainline else {"score": None, "coverage_pct": 0, "components": [], "missing_metrics": ["主线结构"]}),
        "W": {"score": None if width_pct is None else round(rising(width_pct, 25, 75)), "coverage_pct": 100 if width_pct is not None else 0, "components": [{"metric": "上涨家数占比", "value": width_pct}], "missing_metrics": [] if width_pct is not None else ["市场宽度"]},
        "S": seal,
    }
    names = {"R": "接力生态", "P": "赚钱效应", "L": "亏钱风险", "C": "龙头状态", "M": "主线强度", "W": "市场宽度", "S": "封板质量"}
    for key, value in packs.items():
        value.update({"name": names[key], "valid": value.get("score") is not None,
                      "confidence": "high" if value.get("coverage_pct", 0) >= 85 else "medium" if value.get("coverage_pct", 0) >= 65 else "low"})
    return packs


def cycle_state(factors: dict, history: dict, small: dict) -> tuple[dict, list[str]]:
    score = lambda key: factors.get(key, {}).get("score")
    r, p, l, c, m = (score(key) for key in ("R", "P", "L", "C", "M"))
    valid = sum(value is not None for value in (r, p, l, c, m))
    sessions = history.get("sessions") or []
    tags = []
    if valid < 3:
        return {"confirmed_large_cycle": None, "stage": "数据不足 / 暂无法判断", "candidate_large_cycle": None, "candidate_stage": None, "confirmation_progress": {"current": 0, "required": 3, "stage": "insufficient_data"}, "confidence": "low", "trend": "—"}, tags
    extreme_risk = (l or 0) >= 75 or ((sessions[-1].get("limit_down_count", 0) if sessions else 0) >= 15 and (r or 100) < 40)
    if extreme_risk or ((l or 0) >= 60 and (r or 100) < 45 and (p or 100) < 45):
        large, stage, trend = "下降退潮", "退潮加速" if extreme_risk else "退潮确认", "↓"
    elif (c or 0) >= 62 and (m or 0) >= 52 and (r or 0) >= 48 and (p or 0) >= 50 and (l or 100) < 55:
        large, trend = "上升周期", "↑"
        if (c or 0) >= 75 and (m or 0) >= 68 and (r or 0) >= 65 and (p or 0) >= 65:
            stage = "主升"
        elif (m or 0) >= 60 and (r or 0) >= 55:
            stage = "发酵"
        else:
            stage = "启动确认"
    elif (c or 0) >= 60 and ((r or 100) < 48 or (p or 100) < 48):
        large, stage, trend = "高位震荡", "首次分歧", "↓"
        tags.append("核心抱团")
    else:
        large, trend = "低位混沌", "→"
        improving = sum(value is not None and value >= 52 for value in (r, p, c, m))
        stage = "新周期试错" if improving >= 3 and (l or 100) < 60 else "方向不明"
        if stage == "新周期试错": tags.append("新周期试错")
    if sessions:
        current_up = sessions[-1].get("limit_up_count")
        prior = sorted(row.get("limit_up_count") for row in sessions[:-1] if row.get("limit_up_count") is not None)
        if prior and current_up is not None and current_up <= prior[max(0, int(len(prior) * .2) - 1)] and (l or 0) >= 55:
            tags.append("冰点")
    confidence = "high" if len(sessions) >= 20 and valid == 5 else "medium" if len(sessions) >= 10 and valid >= 4 else "low"
    return {"confirmed_large_cycle": large, "stage": stage, "candidate_large_cycle": None, "candidate_stage": None, "confirmation_progress": {"current": 3 if confidence == "high" else 2 if confidence == "medium" else 1, "required": 3, "stage": "confirmed" if confidence == "high" else "history_accumulating"}, "confidence": confidence, "trend": trend}, tags


def exposure_and_switch(cycle: dict, small: dict, factors: dict) -> tuple[dict, dict]:
    key = (cycle.get("confirmed_large_cycle"), cycle.get("stage"))
    bands = {
        ("下降退潮", "退潮加速"): ([0, 10], 10, 5),
        ("下降退潮", "退潮确认"): ([0, 20], 20, 15),
        ("下降退潮", "止跌修复"): ([10, 20], 25, 20),
        ("低位混沌", "方向不明"): ([10, 30], 35, 25),
        ("低位混沌", "新周期试错"): ([20, 35], 40, 35),
        ("上升周期", "启动确认"): ([35, 55], 60, 50),
        ("上升周期", "发酵"): ([50, 70], 75, 65),
        ("上升周期", "主升"): ([65, 85], 90, 78),
        ("高位震荡", "首次分歧"): ([40, 60], 65, 52),
    }
    base, hard_cap, base_switch = bands.get(key, ([0, 0], 0, 0))
    small_adjust = {"强修复": 8, "修复": 4, "弱修复": 4, "回流": 8, "一致": 3, "弱分歧": -5, "分歧": -5, "强分歧": -10, "转弱": -12, "恐慌释放": -8}.get(small.get("stage"), 0)
    structural = [factors.get(name, {}).get("score") for name in ("R", "P", "C", "M")]
    good = sum(value is not None and value >= 60 for value in structural)
    weak = sum(value is not None and value < 40 for value in structural)
    structure_adjust = 8 if good == 4 else 5 if good == 3 else 2 if good == 2 else -8 if weak >= 3 else -4 if weak >= 2 else 0
    loss = factors.get("L", {}).get("score")
    risk_penalty = 20 if loss is not None and loss >= 75 else 10 if loss is not None and loss >= 60 else 0
    switch = round(clamp(base_switch + small_adjust + structure_adjust - risk_penalty))
    final_low = max(0, min(hard_cap, base[0] + min(small_adjust, 0)))
    final_high = max(final_low, min(hard_cap, base[1] + small_adjust - risk_penalty))
    confidence_cap = 100 if cycle.get("confidence") == "high" else 70 if cycle.get("confidence") == "medium" else 40
    hard_cap = min(hard_cap, confidence_cap)
    final_low, final_high = min(final_low, hard_cap), min(final_high, hard_cap)
    level = "防守" if switch <= 15 else "谨慎" if switch <= 30 else "试错" if switch <= 45 else "可参与" if switch <= 60 else "积极" if switch <= 75 else "强势" if switch <= 90 else "极端一致"
    exposure = {"base_range": base, "small_cycle_adjustment": small_adjust, "structure_adjustment": structure_adjust, "risk_penalty": risk_penalty, "confidence_cap": confidence_cap, "final_range": [final_low, final_high], "hard_cap": hard_cap}
    market_switch = {"value": switch, "level": level, "model_version": "cycle_switch_v1", "confidence": cycle.get("confidence"), "limiting_reasons": [name for name, value in (("亏钱效应偏高", loss is not None and loss >= 60), ("历史样本不足20日", cycle.get("confidence") != "high")) if value]}
    return exposure, market_switch


def main() -> None:
    emotion, history, intraday = read(EMOTION), read(HISTORY), read(INTRADAY)
    leader, ecology = leader_strength(emotion), market_ecology(emotion)
    themes = theme_cycles(emotion)
    legacy_large, small = large_cycle(emotion, history, leader, ecology), small_cycle(intraday)
    mainline = themes[0] if themes else None
    factors = factor_pack(emotion, history, leader, ecology, themes)
    cycle, tags = cycle_state(factors, history, small)
    exposure, market_switch = exposure_and_switch(cycle, small, factors)
    large = {**cycle, "score": market_switch.get("value"), "method": "状态机 + 结构门槛 + 连续性；不使用均线", "tags": tags,
             "supporting_factors": legacy_large.get("supporting_factors", []), "pressure_factors": legacy_large.get("pressure_factors", []),
             "conclusion": f"{cycle.get('confirmed_large_cycle') or '数据不足'}·{cycle.get('stage')}"}
    missing = sorted(set(leader.get("missing_metrics", []) + ecology.get("missing_metrics", []) + ([] if small.get("timeline_ready") else ["完整日内情绪时间轴"])))
    def component(name):
        return next((c.get("normalized") for c in ecology.get("components", []) if c.get("metric") == name), None)
    payload = {
        "schema_version": 2, "ok": True, "trade_date": emotion.get("trade_date"), "generated_at": datetime.now(timezone.utc).isoformat(),
        "principles": ["不使用5/10/20日均线判断情绪周期", "龙头强度与市场生态分开计算", "周期切换采用评分、结构、连续性三层确认", "缺失数据不补造、不按0分处理"],
        "dashboard": {"market_composite": market_switch.get("value"), "leader_strength": leader.get("score"), "market_ecology": ecology.get("score"), "market_width": factors["W"].get("score"), "relay_ecology": factors["R"].get("score"), "mainline_strength": factors["M"].get("score"), "loss_effect": factors["L"].get("score"), "structure_label": matrix_label(leader, ecology)},
        "large_cycle": large, "medium_cycle": {"mainline": mainline, "themes": themes}, "small_cycle": small,
        "cycle": {**cycle, "small_cycle_state": small.get("stage"), "small_cycle_path": [row.get("node") for row in small.get("timeline", [])], "tags": tags},
        "factors": factors, "exposure": exposure, "market_switch": market_switch,
        "leader_ecology_matrix": {"leader_strength": leader, "market_ecology": ecology, "label": matrix_label(leader, ecology)},
        "data_quality": {"missing": missing, "analysis_allowed": sum(factors[k].get("valid", False) for k in ("R", "P", "L", "C", "M")) >= 3, "small_cycle_allowed": bool(small.get("timeline_ready")), "complete_trading_days": len(history.get("sessions") or [])},
    }
    write(OUTPUT, payload)
    print(json.dumps({"trade_date": payload["trade_date"], "large_cycle": large.get("stage"), "structure": payload["dashboard"]["structure_label"], "mainline": mainline.get("theme") if mainline else None, "small_cycle": small.get("stage"), "missing": missing}, ensure_ascii=False))


if __name__ == "__main__":
    main()
