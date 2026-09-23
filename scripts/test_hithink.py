import unittest
from unittest.mock import patch

from hithink_client import HithinkError, all_quotes, pool
from fetch_hithink import build_market


class SourceIntegrity(unittest.TestCase):
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

    def test_missing_quote_is_unknown_not_flat(self):
        index = [{"thscode": code, "ticker": code[:6], "turnover": 1}
                 for code in ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH")]
        quote_rows = [{"thscode": "123456.SZ", "price_change_ratio_pct": 1, "turnover": 100},
                      {"thscode": "654321.SZ", "price_change_ratio_pct": None, "turnover": None}]
        with patch("fetch_hithink.pool", side_effect=[[{
                "ticker": "123456", "thscode": "123456.SZ", "name": "甲",
                "continue_day_cnt": 2, "limit_up_reason": "测试", "limit_up_time": "09:40"}], [], []]), \
             patch("fetch_hithink.all_quotes", return_value=(quote_rows, 2)), \
             patch("fetch_hithink.get", return_value={"item": index}):
            market, facts = build_market("2026-09-22")
        self.assertIsNone(market["breadth"]["flat"])
        self.assertEqual(market["breadth"]["unknown"], 1)
        self.assertIsNone(facts["market_amount_cny"])
        self.assertEqual(market["limitUps"][0]["limitReason"], "测试")


if __name__ == "__main__":
    unittest.main()
