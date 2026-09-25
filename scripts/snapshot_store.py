"""Append-only market snapshot storage and health metadata."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN = timezone(timedelta(hours=8))
DATA = Path("data")
SESSIONS = ((9, 35, "open_check"), (10, 30, "morning_confirm"),
            (11, 30, "morning_close"), (13, 45, "afternoon_check"),
            (14, 30, "tail_check"), (15, 0, "close"))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def session_for(stamp: datetime) -> str:
    minute = stamp.hour * 60 + stamp.minute
    target = min(SESSIONS, key=lambda row: abs(minute - row[0] * 60 - row[1]))
    return target[2] if abs(minute - target[0] * 60 - target[1]) <= 20 else "manual"


def _next_revision(directory: Path, base_id: str) -> tuple[int, str | None]:
    existing = sorted(directory.glob(f"{base_id}-r*.json"))
    if not existing:
        return 0, None
    previous = existing[-1].stem
    return int(previous.rsplit("-r", 1)[1]) + 1, previous


def _missing_fields(market: dict) -> list[str]:
    breadth = market.get("breadth") or {}
    required = {
        "indices": market.get("indices"),
        "breadth.up": breadth.get("up") if breadth.get("up") is not None else breadth.get("known_up"),
        "breadth.down": breadth.get("down") if breadth.get("down") is not None else breadth.get("known_down"),
        "breadth.flat": breadth.get("flat") if breadth.get("flat") is not None else breadth.get("known_flat"),
        "breadth.totalAmount": breadth.get("totalAmount"),
        "limitUpCount": market.get("limitUpCount"),
        "limitDownCount": market.get("limitDownCount"),
        "brokenCount": market.get("brokenCount"),
        "limitUps": market.get("limitUps"),
    }
    return [name for name, value in required.items()
            if value is None or (isinstance(value, list) and not value)]


def persist_snapshot(market: dict, source_payload: dict,
                     captured_at: datetime | None = None,
                     revision_reason: str | None = None) -> dict:
    stamp = (captured_at or datetime.now(CN)).astimezone(CN)
    trade_date = str(market["tradeDate"])
    session = session_for(stamp)
    base_id = f"{trade_date}-{stamp:%H%M%S}"
    raw_dir = DATA / "raw" / trade_date
    revision, supersedes = _next_revision(raw_dir, base_id)
    snapshot_id = f"{base_id}-r{revision}"
    checksum = hashlib.sha256(_canonical(source_payload)).hexdigest()
    raw_path = raw_dir / f"{snapshot_id}.json"
    clean_path = DATA / "clean" / trade_date / f"{snapshot_id}.json"
    missing = _missing_fields(market)
    data_status = "complete" if not missing else "partial"
    raw = {
        "snapshot_id": snapshot_id, "market_date": trade_date,
        "captured_at": stamp.isoformat(), "session": session,
        "source": market.get("source"), "schema_version": "market_raw_v1",
        "status": "success", "checksum_algorithm": "sha256",
        "payload_checksum": checksum, "revision": revision,
        "supersedes": supersedes, "revision_reason": revision_reason,
        "original_frozen": revision == 0, "payload": source_payload,
    }
    clean = {
        "snapshot_id": snapshot_id, "raw_snapshot_id": snapshot_id,
        "market_date": trade_date, "captured_at": stamp.isoformat(),
        "session": session, "source": market.get("source"),
        "schema_version": "market_clean_v1", "clean_version": "market_clean_v1",
        "generated_at": datetime.now(CN).isoformat(), "status": data_status,
        "missing_fields": missing, "revision": revision,
        "supersedes": supersedes, "market": market,
    }
    _write(raw_path, raw)
    _write(clean_path, clean)
    _write(DATA / "clean" / "latest.json", clean)
    index_path = DATA / "snapshots" / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        index = {"schema_version": "snapshot_index_v1", "snapshots": []}
    index["snapshots"] = [row for row in index.get("snapshots", [])
                          if row.get("snapshot_id") != snapshot_id]
    index["snapshots"].append({
        "snapshot_id": snapshot_id, "market_date": trade_date,
        "captured_at": stamp.isoformat(), "session": session,
        "status": data_status, "revision": revision,
        "raw_path": raw_path.as_posix(), "clean_path": clean_path.as_posix(),
    })
    index["snapshots"].sort(key=lambda row: (row["captured_at"], row["revision"]))
    index["updated_at"] = datetime.now(CN).isoformat()
    _write(index_path, index)
    health = {
        "schema_version": "market_status_v1", "market_date": trade_date,
        "latest_snapshot_id": snapshot_id, "latest_snapshot": stamp.isoformat(),
        "session": session, "source": market.get("source"),
        "status": data_status, "missing_fields": missing, "last_error": None,
        "datasets": {"market": {"status": data_status,
                                  "updated_at": stamp.isoformat(),
                                  "missing_fields": missing}},
    }
    _write(DATA / "status_latest.json", health)
    return health


def persist_failure(error: Exception, trade_date: str,
                    captured_at: datetime | None = None) -> dict:
    stamp = (captured_at or datetime.now(CN)).astimezone(CN)
    base_id = f"{trade_date}-{stamp:%H%M%S}"
    raw_dir = DATA / "raw" / trade_date
    revision, supersedes = _next_revision(raw_dir, base_id)
    snapshot_id = f"{base_id}-r{revision}"
    raw = {
        "snapshot_id": snapshot_id, "market_date": trade_date,
        "captured_at": stamp.isoformat(), "session": session_for(stamp),
        "source": "同花顺官方 Financial-API", "schema_version": "market_raw_v1",
        "status": "failed", "checksum_algorithm": "sha256",
        "payload_checksum": None, "revision": revision, "supersedes": supersedes,
        "revision_reason": None, "original_frozen": revision == 0,
        "error": f"{type(error).__name__}: {str(error)[:500]}", "payload": None,
    }
    _write(raw_dir / f"{snapshot_id}.json", raw)
    try:
        latest_clean = json.loads((DATA / "clean" / "latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        latest_clean = {}
    has_frozen_market = bool(latest_clean.get("snapshot_id") and latest_clean.get("market_date"))
    if has_frozen_market:
        # A failed refresh must not turn an existing frozen trading-day snapshot
        # into "no data". Keep the failed raw attempt for audit and expose the
        # last valid market as stale until a fresh successful snapshot arrives.
        health = {
            "schema_version": "market_status_v1",
            "market_date": latest_clean["market_date"],
            "latest_snapshot_id": latest_clean["snapshot_id"],
            "latest_snapshot": latest_clean.get("captured_at"),
            "session": latest_clean.get("session"),
            "source": latest_clean.get("source") or "同花顺官方 Financial-API",
            "status": "stale", "missing_fields": latest_clean.get("missing_fields") or [],
            "last_error": raw["error"],
            "last_attempt": {"snapshot_id": snapshot_id, "market_date": trade_date,
                             "captured_at": stamp.isoformat(), "status": "failed"},
            "datasets": {"market": {"status": "stale",
                                      "updated_at": latest_clean.get("captured_at"),
                                      "missing_fields": latest_clean.get("missing_fields") or []}},
        }
    else:
        health = {
            "schema_version": "market_status_v1", "market_date": trade_date,
            "latest_snapshot_id": snapshot_id, "latest_snapshot": stamp.isoformat(),
            "session": session_for(stamp), "source": "同花顺官方 Financial-API",
            "status": "unavailable", "missing_fields": ["market"],
            "last_error": raw["error"],
            "datasets": {"market": {"status": "unavailable",
                                      "updated_at": stamp.isoformat(),
                                      "missing_fields": ["market"]}},
        }
    _write(DATA / "status_latest.json", health)
    return health


def persist_skipped(trade_date: str, reason: str,
                    captured_at: datetime | None = None) -> dict:
    """Keep the latest frozen market visible when a newer date is skipped."""
    stamp = (captured_at or datetime.now(CN)).astimezone(CN)
    try:
        latest_clean = json.loads((DATA / "clean" / "latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        latest_clean = {}
    if not (latest_clean.get("snapshot_id") and latest_clean.get("market_date")):
        return persist_failure(RuntimeError(reason), trade_date, stamp)
    health = {
        "schema_version": "market_status_v1",
        "market_date": latest_clean["market_date"],
        "latest_snapshot_id": latest_clean["snapshot_id"],
        "latest_snapshot": latest_clean.get("captured_at"),
        "session": latest_clean.get("session"),
        "source": latest_clean.get("source") or "同花顺官方 Financial-API",
        "status": "stale", "missing_fields": latest_clean.get("missing_fields") or [],
        "last_error": None,
        "last_attempt": {"market_date": trade_date, "captured_at": stamp.isoformat(),
                         "status": "skipped", "reason": reason},
        "datasets": {"market": {"status": "stale",
                                  "updated_at": latest_clean.get("captured_at"),
                                  "missing_fields": latest_clean.get("missing_fields") or []}},
    }
    _write(DATA / "status_latest.json", health)
    return health
