#!/usr/bin/env python3
"""Build explainable short-term cycle judgments from collected facts."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

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
    first_minutes = int(first[:2]) * 60 + int(first[2:4]) if len(first) >= 4 and first.isdigit() else None
    theme_count = next((t.get("limit_up_count") for t in emotion.get("themes", []) if t.get("name") == leader.get("theme")), None)
    result = weighted([
        {"metric": "龙头高度", "value": leader.get("height"), "weight": 30, "normalized": rising(leader.get("height"), 2, 7)},
        {"metric": "封板稳定性", "value": leader.get("open_count"), "weight": 25, "normalized": falling(leader.get("open_count"), 0, 5)},
        {"metric": "首次封板主动性", "value": leader.get("first_limit"), "weight": 20, "normalized": falling(first_minutes, 570, 870)},
        {"metric": "封单/成交额", "value": None if seal_ratio is None else round(seal_ratio, 2), "weight": 15, "normalized": rising(seal_ratio, 1, 50)},
        {"metric": "龙头所在题材涨停数", "value": theme_count, "weight": 10, "normalized": rising(theme_count, 1, 8)},
    ])
    result.update({"leader": {key: leader.get(key) for key in ("code", "name", "theme", "height")}, "label": "强" if result["score"] >= 65 else "弱"})
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
    result["label"] = "强" if result["score"] is not None and result["score"] >= 65 else "弱"
    return result


def matrix_label(leader: dict, ecology: dict) -> str:
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
    if score is None:
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
    confidence = "high" if coverage >= 85 and len(sessions) >= 10 else "medium" if coverage >= 65 and len(sessions) >= 5 else "low"
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
    snapshots = intraday.get("snapshots") or []
    ready = bool((intraday.get("data_quality") or {}).get("timeline_ready")) and len(snapshots) >= 6
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
        timeline.append({"time": row.get("time"), "node": node, "facts": row})
        previous = row
    return {"stage": timeline[-1]["node"], "score": None, "trend": "→", "timeline_ready": True, "snapshot_count": len(snapshots), "timeline": timeline, "note": "日内节点不直接改写大周期。"}


def main() -> None:
    emotion, history, intraday = read(EMOTION), read(HISTORY), read(INTRADAY)
    leader, ecology = leader_strength(emotion), market_ecology(emotion)
    themes = theme_cycles(emotion)
    large, small = large_cycle(emotion, history, leader, ecology), small_cycle(intraday)
    mainline = themes[0] if themes else None
    missing = sorted(set(leader.get("missing_metrics", []) + ecology.get("missing_metrics", []) + ([] if small.get("timeline_ready") else ["完整日内情绪时间轴"])))
    def component(name):
        return next((c.get("normalized") for c in ecology.get("components", []) if c.get("metric") == name), None)
    payload = {
        "schema_version": 1, "ok": True, "trade_date": emotion.get("trade_date"), "generated_at": datetime.now(timezone.utc).isoformat(),
        "principles": ["不使用5/10/20日均线判断情绪周期", "龙头强度与市场生态分开计算", "周期切换采用评分、结构、连续性三层确认", "缺失数据不补造、不按0分处理"],
        "dashboard": {"market_composite": large.get("score"), "leader_strength": leader.get("score"), "market_ecology": ecology.get("score"), "market_width": component("市场上涨宽度"), "relay_ecology": component("连板晋级率"), "mainline_strength": mainline.get("score") if mainline else None, "loss_effect": None if component("中位股大亏比例") is None else round(100 - component("中位股大亏比例")), "structure_label": matrix_label(leader, ecology)},
        "large_cycle": large, "medium_cycle": {"mainline": mainline, "themes": themes}, "small_cycle": small,
        "leader_ecology_matrix": {"leader_strength": leader, "market_ecology": ecology, "label": matrix_label(leader, ecology)},
        "data_quality": {"missing": missing, "analysis_allowed": bool(large.get("score") is not None and mainline), "small_cycle_allowed": bool(small.get("timeline_ready"))},
    }
    write(OUTPUT, payload)
    print(json.dumps({"trade_date": payload["trade_date"], "large_cycle": large.get("stage"), "structure": payload["dashboard"]["structure_label"], "mainline": mainline.get("theme") if mainline else None, "small_cycle": small.get("stage"), "missing": missing}, ensure_ascii=False))


if __name__ == "__main__":
    main()
