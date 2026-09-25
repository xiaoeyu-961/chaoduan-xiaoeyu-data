import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import snapshot_store


class SnapshotStoreTests(unittest.TestCase):
    def test_session_names_are_exact(self):
        cn = timezone(timedelta(hours=8))
        self.assertEqual(snapshot_store.session_for(datetime(2026, 9, 24, 9, 35, tzinfo=cn)), "open_check")
        self.assertEqual(snapshot_store.session_for(datetime(2026, 9, 24, 14, 30, tzinfo=cn)), "tail_check")
        self.assertEqual(snapshot_store.session_for(datetime(2026, 9, 24, 12, 20, tzinfo=cn)), "manual")

    def test_append_only_revision_and_checksum(self):
        market = {"tradeDate": "2026-09-24", "source": "同花顺官方 Financial-API",
                  "indices": [{"code": "000001"}],
                  "breadth": {"up": 1, "down": 1, "flat": 1, "totalAmount": 3},
                  "limitUpCount": 1, "limitDownCount": 1, "brokenCount": 0,
                  "limitUps": [{"code": "000001"}]}
        stamp = datetime(2026, 9, 24, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(snapshot_store, "DATA", Path(tmp)):
            first = snapshot_store.persist_snapshot(market, {"value": 1}, stamp)
            second = snapshot_store.persist_snapshot(market, {"value": 2}, stamp, "数据源延迟后补齐")
            self.assertEqual(first["latest_snapshot_id"], "2026-09-24-150000-r0")
            self.assertEqual(second["latest_snapshot_id"], "2026-09-24-150000-r1")
            raw = json.loads((Path(tmp) / "raw/2026-09-24/2026-09-24-150000-r1.json").read_text())
            self.assertEqual(raw["supersedes"], "2026-09-24-150000-r0")
            self.assertEqual(len(raw["payload_checksum"]), 64)

    def test_failed_refresh_preserves_last_frozen_market(self):
        market = {"tradeDate": "2026-09-24", "source": "同花顺官方 Financial-API",
                  "indices": [{"code": "000001"}],
                  "breadth": {"up": 1, "down": 1, "flat": 1, "totalAmount": 3},
                  "limitUpCount": 1, "limitDownCount": 1, "brokenCount": 0,
                  "limitUps": [{"code": "000001"}]}
        cn = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmp, patch.object(snapshot_store, "DATA", Path(tmp)):
            valid = snapshot_store.persist_snapshot(
                market, {"value": 1}, datetime(2026, 9, 24, 15, 0, tzinfo=cn))
            health = snapshot_store.persist_failure(
                RuntimeError("timeout"), "2026-09-25",
                datetime(2026, 9, 25, 9, 35, tzinfo=cn))
            self.assertEqual(health["status"], "stale")
            self.assertEqual(health["market_date"], "2026-09-24")
            self.assertEqual(health["latest_snapshot_id"], valid["latest_snapshot_id"])
            self.assertEqual(health["last_attempt"]["market_date"], "2026-09-25")


if __name__ == "__main__":
    unittest.main()
