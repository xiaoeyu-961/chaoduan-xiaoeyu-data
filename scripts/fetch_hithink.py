#!/usr/bin/env python3
"""Collect Tonghuashun facts into the existing dashboard contract."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from hithink_client import CN_TZ, HithinkError, all_quotes, get, pool
from snapshot_store import persist_failure, persist_skipped, persist_snapshot

DATA = Path("data")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def same_market_fingerprint(previous: dict, current: dict) -> bool:
    """Detect a provider response that is still frozen on the prior session."""
    old_indices = {
        row.get("name"): (row.get("price"), row.get("change"), row.get("amount"))
        for row in previous.get("indices") or []
    }
    new_indices = {
        row.get("name"): (row.get("price"), row.get("change"), row.get("amount"))
        for row in current.get("indices") or []
    }
    return bool(old_indices and old_indices == new_indices)


def mark_github_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_stock(item: dict, kind: str = "limitUp") -> dict:
    return {
        "code": item.get("ticker"), "thscode": item.get("thscode"),
        "name": item.get("name"), "theme": None,
        "height": item.get("continue_day_cnt") if kind == "limitUp" else None,
        "firstLimit": item.get("limit_up_time") if kind == "limitUp" else item.get("first_limit_time"),
        "lastLimit": item.get("limit_up_time") if kind == "limitUp" else item.get("last_limit_time"),
        "amount": item.get("turnover"), "turnover": item.get("turnover_ratio_pct"),
        "change": item.get("price_change_ratio_pct"),
        "sealAmount": item.get("seal_money"),
        "maxSealAmount": item.get("max_seal_money"),
        "openCount": item.get("open_times"),
        "limitReason": item.get("limit_up_reason"),
        "price": item.get("last_price"),
    }


def build_market(date: str) -> tuple[dict, dict]:
    up_raw = pool("limitUp", date)
    broken_raw = pool("broken", date)
    down_raw = pool("limitDown", date)
    quotes, universe = all_quotes()
    indices_raw = get("/api/a-share-index/prices/snapshot", {
        "thscodes": "000001.SH,399001.SZ,399006.SZ,000688.SH"})
    index_names = {"000001.SH": "上证指数", "399001.SZ": "深证成指",
                   "399006.SZ": "创业板指", "000688.SH": "科创50"}
    indices = [{"code": r.get("ticker"), "name": index_names[r["thscode"]],
                "price": r.get("last_price"), "change": r.get("price_change_ratio_pct"),
                "amount": r.get("turnover"), "source": "同花顺指数行情"}
               for r in indices_raw.get("item", []) if r.get("thscode") in index_names]
    if len(indices) != len(index_names):
        raise HithinkError("index snapshot lacks required indices")
    # The two broad-market index snapshots are the authoritative exchange
    # turnover totals.  Summing every security is stricter than necessary and
    # becomes null as soon as a suspended symbol has no quote.
    exchange_amounts = {
        row["name"]: row.get("amount") for row in indices
        if row.get("name") in ("上证指数", "深证成指")
    }
    index_total_amount = (sum(exchange_amounts.values())
                          if len(exchange_amounts) == 2
                          and all(value is not None for value in exchange_amounts.values())
                          else None)

    up = down = flat = 0
    # Missing changes normally represent suspended, delisted or otherwise
    # non-trading symbols. They are excluded from the advance/decline base,
    # never counted as flat.
    missing_changes = max(universe - len(quotes), 0)
    total_turnover = 0
    turnover_missing = max(universe - len(quotes), 0)
    for quote in quotes:
        change = quote.get("price_change_ratio_pct")
        amount = quote.get("turnover")
        if change is None:
            missing_changes += 1
        elif change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        if amount is None:
            turnover_missing += 1
        else:
            total_turnover += amount
    counted_universe = up + down + flat
    breadth_complete = counted_universe > 0
    turnover_complete = turnover_missing == 0
    quote_by_code = {row.get("thscode"): row for row in quotes}
    limit_ups = [normalize_stock(r) for r in up_raw]
    for row in limit_ups:
        quote = quote_by_code.get(row.get("thscode")) or {}
        if row["amount"] is None:
            row["amount"] = quote.get("turnover")
    limit_ups = sorted(limit_ups,
                       key=lambda r: (-(r["height"] or 0), r["code"] or ""))
    broken = [normalize_stock(r, "broken") for r in broken_raw]
    limit_down = [normalize_stock(r, "limitDown") for r in down_raw]
    generated = datetime.now(timezone.utc).isoformat()
    denominator = len(up_raw) + len(broken_raw)
    market = {
        "schemaVersion": 1, "ok": True,
        "source": "同花顺官方 Financial-API", "sourceMode": "scheduled-snapshot",
        "tradeDate": date, "updatedAt": generated,
        "indices": indices,
        "breadth": {"up": up if breadth_complete else None,
                    "down": down if breadth_complete else None,
                    "flat": flat if breadth_complete else None,
                    "known_up": up, "known_down": down, "known_flat": flat,
                    "unknown": missing_changes, "universe": universe,
                    "countedUniverse": counted_universe,
                    "excludedNoChange": missing_changes,
                    "complete": breadth_complete,
                    "coverage_pct": round((universe - missing_changes) / universe * 100, 2),
                    "usable": universe > 0 and (universe - missing_changes) / universe >= .98,
                    "totalAmount": index_total_amount,
                    "stockAmount": total_turnover if turnover_complete else None,
                    "source": "同花顺全市场行情快照"},
        "limitUpCount": len(limit_ups), "brokenCount": len(broken),
        "limitDownCount": len(limit_down),
        "brokenRate": round(len(broken) / denominator * 100, 2) if denominator else None,
        "highest": limit_ups[0] if limit_ups else None,
        "limitUps": limit_ups, "broken": broken, "limitDown": limit_down,
        "themes": [],
        "quality": {"complete": breadth_complete and turnover_complete,
                    "errors": [], "collector": "hithink-finance",
                    "notes": ["缺少有效涨跌幅的停牌、退市或未交易标的从涨跌家数统计母体中剔除，不计入平盘。",
                              "涨停原因不是概念板块归属；板块需单独使用成分股接口。"]},
    }
    facts = {"source": market["source"], "trade_date": date, "updated_at": generated,
             "market_quote_total": universe,
             "limit_up": {"count": len(up_raw), "complete": True},
             "broken": {"count": len(broken_raw), "complete": True},
             "limit_down": {"count": len(down_raw), "complete": True},
             "auction": None, "sectors": None,
             "market_amount_cny": index_total_amount,
             "market_amount_scope": "上证指数成交额 + 深证成指成交额"}
    return market, facts


def enrich(date: str, market: dict, facts: dict) -> None:
    # Optional requests cannot silently replace core verified market data.
    try:
        ladder = get("/api/a-share/special-data/limit-up-ladder")
        facts["limit_up_ladder"] = {
            "window": ladder.get("window") or {},
            "items": ladder.get("item") or [],
        }
    except HithinkError as exc:
        facts.setdefault("missing", []).append(f"limit_up_ladder: {exc}")
    codes = [r["thscode"] for r in market["limitUps"][:100] if r.get("thscode")]
    if codes:
        try:
            auction = get("/api/a-share/auction/snapshot", {
                "thscodes": ",".join(codes), "stage": "final"})
            facts["auction"] = {"data_status": auction.get("data_status"),
                                "auction_phase": auction.get("auction_phase"),
                                "items": auction.get("item") or []}
        except HithinkError as exc:
            facts.setdefault("missing", []).append(f"auction: {exc}")
        try:
            anomaly = get("/api/a-share/special-data/anomaly-analysis-stock", {
                "thscodes": ",".join(codes[:50])})
            facts["anomaly_analysis"] = anomaly.get("item") or []
        except HithinkError as exc:
            facts.setdefault("missing", []).append(f"anomaly_analysis: {exc}")
    try:
        catalog = get("/api/a-share-index/catalog/ths-index-list", {"tag": "cn_concept"})
        items = catalog.get("item") or []
        quotes = []
        for start in range(0, len(items), 50):
            codes = ",".join(row["thscode"] for row in items[start:start + 50])
            quotes.extend(get("/api/a-share-index/prices/snapshot", {"thscodes": codes}).get("item") or [])
        if len({row.get("thscode") for row in quotes}) != len(items):
            raise HithinkError("concept index snapshot incomplete")
        names = {row["thscode"]: row["name"] for row in items}
        # Always include concepts explicitly named by the official limit-up
        # reason, in addition to the strongest 30 concepts. Querying every
        # concept's constituents every ten minutes is unnecessarily expensive,
        # while this targeted expansion covers the current leaders directly.
        reason_names = {str(row.get("limitReason") or "").strip()
                        for row in market["limitUps"] if row.get("limitReason")}
        reason_codes = {code for code, name in names.items()
                        if name in reason_names or any(name in reason or reason in name
                                                       for reason in reason_names)}
        strongest = sorted(quotes, key=lambda row: row.get("price_change_ratio_pct")
                           if row.get("price_change_ratio_pct") is not None else float("-inf"),
                           reverse=True)[:30]
        selected_codes = {row["thscode"] for row in strongest} | reason_codes
        leaders = [row for row in quotes if row.get("thscode") in selected_codes]
        up_codes = {row["thscode"]: row for row in market["limitUps"]}
        sectors = []
        for leader in leaders:
            thscode = leader["thscode"]
            try:
                members = get("/api/a-share-index/constituents/ths-stock-list", {
                    "thscode": thscode}).get("item") or []
                winners = [up_codes[row["thscode"]] for row in members if row.get("thscode") in up_codes]
                for winner in winners:
                    if not winner.get("theme"):
                        winner["theme"] = names[thscode]
                heights = Counter(row["height"] for row in winners)
                sectors.append({"code": thscode, "name": names[thscode],
                                "change_pct": leader.get("price_change_ratio_pct"),
                                "turnover_cny": leader.get("turnover"),
                                "member_count": len(members), "limit_up_count": len(winners),
                                "board_counts": dict(heights),
                                "limit_up_stocks": [{"code": row["code"], "name": row["name"],
                                                     "height": row["height"]} for row in winners],
                                "source": "同花顺概念指数及当前成分股"})
            except HithinkError as exc:
                facts.setdefault("missing", []).append(f"sector_members {thscode}: {exc}")
        facts["sectors"] = {"category": "cn_concept", "catalog_count": len(items),
                            "quotes_count": len(quotes), "top_by_change_pct": sectors,
                            "matched_reason_concepts": len(reason_codes),
                            "strength_ready": True,
                            "missing": "跨日板块持续性仍需由每日快照累积。"}
        # Feed the verified sector/member intersection into the normal market
        # contract so the emotion builder does not discard official sectors.
        market["themes"] = [{
            "code": row["code"], "name": row["name"],
            "change_pct": row.get("change_pct"),
            "turnover_cny": row.get("turnover_cny"),
            "member_count": row.get("member_count"),
            "limitUpCount": row.get("limit_up_count"),
            "maxHeight": max((stock.get("height") or 0 for stock in row.get("limit_up_stocks", [])), default=0),
            "leaders": row.get("limit_up_stocks", []),
        } for row in sectors]
    except HithinkError as exc:
        facts.setdefault("missing", []).append(f"sector_catalog: {exc}")


def main() -> None:
    date = os.environ.get("MARKET_TRADE_DATE") or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    try:
        previous_market = read_json(DATA / "market_latest.json")
        market, facts = build_market(date)
        empty_pools = not market["limitUps"] and not market["broken"] and not market["limitDown"]
        requested_new_date = bool(previous_market.get("tradeDate") and previous_market.get("tradeDate") < date)
        if requested_new_date and empty_pools and same_market_fingerprint(previous_market, market):
            # Holiday or stale-provider response: preserve the last valid trade
            # date and do not create a false zero-market snapshot.
            mark_github_output("market_skipped", "true")
            persist_skipped(date, "non_trading_or_stale_provider")
            print(json.dumps({"trade_date": date, "skipped": True,
                              "reason": "non_trading_or_stale_provider",
                              "preserved_trade_date": previous_market.get("tradeDate")}, ensure_ascii=False))
            return
        enrich(date, market, facts)
        save(Path(os.environ.get("MARKET_OUTPUT", DATA / "market_latest.json")), market)
        save(Path(os.environ.get("HITHINK_OUTPUT", DATA / "hithink_latest.json")), facts)
        health = persist_snapshot(market, {
            "collector": "hithink-finance", "market": market,
            "source_facts": facts,
        })
    except Exception as exc:
        persist_failure(exc, date)
        raise
    print(json.dumps({"trade_date": date, "source": market["source"],
                      "limit_up_count": market["limitUpCount"],
                      "quote_universe": facts["market_quote_total"],
                      "breadth_complete": market["breadth"]["complete"],
                      "optional_missing": facts.get("missing", []),
                      "snapshot_id": health["latest_snapshot_id"],
                      "snapshot_status": health["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
