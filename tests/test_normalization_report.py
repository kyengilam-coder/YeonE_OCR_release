import unittest
from yeone_ocr.normalization_report import render_normalization_report


class NormalizationReport(unittest.TestCase):
    def test_uncertain_choice_and_original_context_are_visible_and_escaped(self):
        report = {"complete": False, "output": "책.hwpx", "summary": {"review": 1},
                  "review": [{"page": 2, "before": "術", "candidates": ["術", "朮"],
                              "original_context": "<script>alert(1)</script>白術", "reason": "문맥 확인"}],
                  "decisions": [{"source": "術", "candidates": ["術", "朮"], "choice": None}],
                  "blocks": [{"block": 0, "before": "学術", "after": "學術"}]}
        html = render_normalization_report(report)
        self.assertIn("문맥 교정 확인 필요", html)
        self.assertIn("PDF 2쪽", html)
        self.assertIn("보류 · 원문 유지", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("學術", html)

    def test_disabled_normalization_is_not_shown_as_applied(self):
        html = render_normalization_report({"enabled": False})
        self.assertIn("자동 교정 생략", html)
        self.assertNotIn("교정 처리 완료", html)
