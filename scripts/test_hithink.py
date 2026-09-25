import unittest
from unittest.mock import patch

from hithink_client import HithinkError, all_quotes, pool
from fetch_hithink import build_market
from fetch_emotion import merged_theme_structure, normalize_trading_dates
from build_cycle_analysis import leader_strength


class SourceIntegrity(unittest.TestCase):
    def test_history_uses_exact_trading_dates(self):
        items = [{"date": value} for value in (
            "20260921", "20260922", "20260923", "20260924", "bad")]
        self.assertEqual(
            normalize_trading_dates(items, "2026-09-23", 2),
            ["2026-09-22", "2026-09-23"],
        )

    def test_pool_requires_all_pages(self):
        first = {"pagination": {"total": 3, "pages": 2},
                 "item": [{"thscode": "a"}, {"thscode": "b"}]}
        second = {"pagination": {"total": 3, "pages": 2},
                  "item": [{"thscode": "c"}]}
        with patch("hithink_client.get", side_effect=[first, second]):
            self.assertEqual(len(pool("limitUp", "2026-09-22")), 3)
        second["item"] = []
        with patch("hithink_client.get", side_effect=[first, second]):
            with self.assertRaises(HithinkError):
                pool("limitUp", "2026-09-22")

    def test_missing_quote_is_excluded_not_flat(self):
        index = [{"thscode": code, "ticker": code[:6], "turnover": 1}
                 for code in ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH")]
        quote_rows = [{"thscode": "123456.SZ", "price_change_ratio_pct": 1, "turnover": 100},
                      {"thscode": "654321.SZ", "price_change_ratio_pct": None, "turnover": None}]
        with patch("fetch_hithink.pool", side_effect=[[{
                "ticker": "123456", "thscode": "123456.SZ", "name": "甲",
                "continue_day_cnt": 2, "limit_up_reason": "测试", "limit_up_time": "09:40",
                "seal_money": 80, "max_seal_money": 100}], [], []]), \
             patch("fetch_hithink.all_quotes", return_value=(quote_rows, 2)), \
             patch("fetch_hithink.get", return_value={"item": index}):
            market, facts = build_market("2026-09-22")
        self.assertEqual(market["breadth"]["flat"], 0)
        self.assertEqual(market["breadth"]["up"], 1)
        self.assertEqual(market["breadth"]["unknown"], 1)
        self.assertEqual(market["breadth"]["excludedNoChange"], 1)
        self.assertEqual(market["breadth"]["countedUniverse"], 1)
        self.assertTrue(market["breadth"]["complete"])
        self.assertEqual(facts["market_amount_cny"], 2)
        self.assertEqual(market["limitUps"][0]["limitReason"], "测试")
        self.assertEqual(market["limitUps"][0]["maxSealAmount"], 100)
        self.assertEqual(market["limitUps"][0]["amount"], 100)

    def test_leader_quality_uses_seal_retention_and_reason_breadth(self):
        market = {"themes": [], "limitUps": [
            {"code": "1", "name": "甲", "height": 4, "limitReason": "机器人"},
            {"code": "2", "name": "乙", "height": 1, "limitReason": "机器人"},
        ]}
        themes = merged_theme_structure(market)
        emotion = {"themes": themes, "leader_candidates": [{
            "code": "1", "name": "甲", "theme": "机器人", "height": 4,
            "open_count": None, "seal_retention_pct": 80,
            "first_limit": "09:35", "seal_amount": 80, "amount": 100,
        }]}
        result = leader_strength(emotion)
        self.assertNotIn("封板稳定性", result["missing_metrics"])
        self.assertNotIn("龙头所在题材涨停数", result["missing_metrics"])
        self.assertEqual(next(row for row in themes if row["name"] == "机器人")["limit_up_count"], 2)


if __name__ == "__main__":
    unittest.main()
