"""제공사를 바꿔 이어 읽을 때 완료 기록과 비용 근거를 구분한다."""
import unittest

from yeone_ocr.estimate import estimate, resolve_price


class ProviderEstimates(unittest.TestCase):
    def estimate(self, **kwargs):
        return estimate("unused.pdf", [1, 2], page_size_px=(900, 1300), guide_chars=13000, **kwargs)

    def test_other_provider_unknown_prices_have_no_gemini_fallback(self):
        result = self.estimate(reader="openai", model="gpt-test", second_model="auto")       # 요금표에 없는 OpenAI 모델
        self.assertIsNone(result["usd_low"])
        self.assertEqual(result["second_model"], "gpt-test")
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(all(row["usd_low"] is None for row in result["rows"]))
        self.assertFalse(result["use_cache"])
        self.assertTrue(any("참고치" in note for note in result["notes"]))

    def test_new_provider_keeps_done_pages_but_excludes_old_usage(self):
        old = {"pdf_page": 1, "pass1": {}, "reader": "gemini", "model": "gemini-2.5-pro",
               "usage": {"calls": 50, "prompt_tokens": 999999, "output_tokens": 888888}}
        result = self.estimate(reader="openai", model="gpt-4.1", records=[old])
        self.assertEqual(result["already_read"], 1)
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["basis"], "기본값")
        self.assertTrue(any("제외" in note for note in result["notes"]))

    def test_partial_known_prices_do_not_understate_total(self):
        result = self.estimate(model="gemini-2.5-pro", second_model="new-model")
        self.assertIsNotNone(result["rows"][0]["usd_low"])
        self.assertIsNone(result["rows"][1]["usd_low"])
        self.assertIsNone(result["usd_low"])

    def test_non_gemini_actual_usage_and_manual_prices(self):
        record = {"pdf_page": 1, "pass1": {}, "reader": "anthropic", "model": "claude-test",
                  "usage": {"calls": 2, "prompt_tokens": 1000, "output_tokens": 100,
                            "cached_tokens": 900, "by_model": {"claude-test": {
                                "calls": 2, "prompt_tokens": 1000, "output_tokens": 100, "cached_tokens": 900}}}}
        result = self.estimate(reader="anthropic", model="claude-test", records=[record],
                               prices={"claude-test": {"in": 2, "out": 10}})
        self.assertEqual(result["calls"], 2)
        self.assertTrue(result["basis"].startswith("실측"))
        self.assertAlmostEqual(result["usd_low"], (1000 * 2 + 100 * 10) / 1e6 * 0.8)

    def test_unknown_measured_model_is_not_priced_at_primary_rate(self):
        record = {"pdf_page": 1, "pass1": {}, "usage": {"calls": 2, "prompt_tokens": 1000,
                  "output_tokens": 100, "by_model": {"gemini-2.5-pro": {"calls": 1}, "new-model (3차)": {"calls": 1}}}}
        result = self.estimate(model="gemini-2.5-pro", records=[record])
        self.assertIsNone(result["usd_low"])
        self.assertIsNone(result["rows"][1]["usd_low"])

    def test_manual_cache_rate_zero_and_invalid_prices(self):
        self.assertEqual(resolve_price("gpt-test", {"gpt-test": {"in": 2, "out": 10, "cached": 0}})["cached"], 0)
        self.assertIsNone(resolve_price("gpt-test", {"gpt-test": {"in": float("nan"), "out": 10}}))
        self.assertIsNone(resolve_price("gpt-test", {"gpt-test": {"in": -2, "out": 10}}))


if __name__ == "__main__":
    unittest.main()
