"""Vertical column order must use geometry, without changing any glyphs."""
import copy
import json
import unittest
from unittest.mock import patch

from ab_ocr import local_engines, ocr
from ab_ocr.reading_order import VERTICAL_RTL_POLICY, order_vertical_payload, vertical_line_indices


def payload(lines):
    return {"text": "\n".join(text for text, _ in lines),
            "words": [{"text": text, "bbox": box, "confidence": 88 + i, "block": 1,
                       "paragraph": 1, "line": i + 1} for i, (text, box) in enumerate(lines)],
            "engine_version": "synthetic", "engine_metadata": {"reading_order": "engine order"}}


class VerticalOrderTests(unittest.TestCase):
    def test_columns_take_priority_over_their_unequal_top_positions(self):
        original = payload([("左列", [10, 0, 20, 80]), ("中列", [40, 5, 50, 85]),
                            ("右列樂一\ufe00。", [70, 10, 80, 90])])
        snapshot = copy.deepcopy(original)
        ordered = order_vertical_payload(original)
        self.assertEqual(ordered["text"], "右列樂一\ufe00。\n中列\n左列")
        self.assertEqual(ordered["engine_metadata"]["source_line_indices"], [2, 1, 0])
        self.assertEqual(ordered["engine_metadata"]["raw_engine_result"]["words"], original["words"])
        self.assertEqual([w["line"] for w in ordered["words"]], [1, 2, 3])
        self.assertEqual([w["confidence"] for w in ordered["words"]], [90, 89, 88])
        self.assertEqual(original, snapshot)
        ordered["words"][0]["bbox"][0] = 0
        self.assertEqual(ordered["engine_metadata"]["raw_engine_result"]["words"][2]["bbox"][0], 70)

    def test_fragmented_column_with_x_jitter_is_read_top_to_bottom(self):
        data = payload([("右下", [72, 50, 82, 80]), ("左上", [10, 0, 20, 30]),
                        ("右上", [70, 5, 80, 35]), ("左下", [11, 45, 21, 75])])
        self.assertEqual(order_vertical_payload(data)["text"], "右上\n右下\n左上\n左下")

    def test_adjacent_columns_are_not_joined_by_transitive_overlap(self):
        data = payload([("左", [0, 0, 10, 20]), ("中下", [5, 40, 15, 60]),
                        ("右上", [10, 10, 20, 30])])
        self.assertEqual(vertical_line_indices(data["words"]), [2, 1, 0])

    def test_empty_single_and_equal_boxes_are_deterministic(self):
        self.assertEqual(order_vertical_payload(payload([]))["text"], "")
        self.assertEqual(order_vertical_payload(payload([("原文", [0, 0, 10, 30])]))["text"], "原文")
        data = payload([("甲", [0, 0, 10, 30]), ("乙", [0, 0, 10, 30])])
        self.assertEqual(vertical_line_indices(data["words"]), [0, 1])

    def test_unmapped_text_is_rejected_instead_of_lost(self):
        data = payload([("一", [0, 0, 10, 30])])
        data["text"] += "extra"
        with self.assertRaises(ValueError):
            order_vertical_payload(data)

    def test_sparse_character_detections_and_empty_region_keep_all_evidence(self):
        data = payload([("當", [55, 5, 65, 15]), ("讀", [80, 4, 90, 14]),
                        ("書", [81, 20, 91, 30]), ("", [0, 0, 100, 100])])
        data["engine_metadata"]["text_join_policy"] = "engine order; newline between detected boxes"
        ordered = order_vertical_payload(data)
        self.assertEqual(ordered["text"], "讀\n書\n\n當")
        self.assertEqual(ordered["engine_metadata"]["source_line_indices"], [1, 2, 3, 0])
        self.assertEqual(ordered["engine_metadata"]["raw_engine_result"]["words"], data["words"])
        self.assertEqual(ordered["engine_metadata"]["raw_engine_result"]["text_join_policy"],
                         data["engine_metadata"]["text_join_policy"])
        self.assertEqual(ordered["engine_metadata"]["text_join_policy"],
                         "geometry order; newline between detected boxes")


class VerticalAdapterTests(unittest.TestCase):
    def test_local_engines_reorder_only_vertical_through_public_adapter(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        data = payload([("左", [10, 0, 20, 80]), ("右", [70, 5, 80, 90])])
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.png"
            Image.new("RGB", (100, 100), "white").save(source)
            info = {"available": True, "languages": ["chi_tra"], "executable": "unused",
                    "worker": "unused", "model_dir": folder,
                    "models": {"zh_sim_g2.pth": {"sha256": "test auxiliary hash"}},
                    "language_models": {"chi_tra": []}, "installation": {}}
            def run(argv, **_kwargs):
                if engine in ("easyocr", "rapidocr", "ppocrv6"):
                    self.assertEqual(argv[argv.index("--direction") + 1], direction)
                else:
                    self.assertNotIn("--direction", argv)
                Path(argv[argv.index("--output") + 1]).write_text(json.dumps(data), encoding="utf-8")
            with patch.object(local_engines, "inspect_engine", return_value=info), patch.object(ocr, "_run", side_effect=run):
                for engine, direction in ((e, d) for e in ("rapidocr", "easyocr", "ppocrv6")
                                          for d in ("vertical", "horizontal")):
                    with self.subTest(engine=engine, direction=direction):
                        result = ocr.recognize_file(source, engine=engine, languages="chi_tra", direction=direction)
                        self.assertEqual('zh_sim_g2.pth' in result.engine_metadata['model_files'],
                                         engine == 'easyocr' and direction == 'vertical')
                        if direction == "vertical":
                            self.assertEqual(result.text, "右\n左")
                            self.assertEqual(result.engine_metadata["raw_engine_result"]["text"], "左\n右")
                            self.assertEqual(result.engine_metadata["reading_order_policy"], VERTICAL_RTL_POLICY)
                        else:
                            self.assertEqual(result.text, "左\n右")
                            self.assertNotIn("raw_engine_result", result.engine_metadata)


if __name__ == "__main__":
    unittest.main()
