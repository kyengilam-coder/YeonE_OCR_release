"""Line-image samples for later fine-tuning: cut from the engine's own boxes, labelled with confirmed text."""
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw
from ab_ocr.ocr import RecognitionResult
from yeone_ocr import glyph_samples as samples
from yeone_ocr.comparison import ComparisonProject


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "samples"
        self.source = self.root / "synthetic.png"
        with Image.new("RGB", (400, 200), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 20, 90, 45), fill="black")
            draw.rectangle((40, 60, 90, 85), fill="gray")
            image.save(self.source)
        self.project = ComparisonProject.open(self.source)

    def add(self, engine="ppocrv6", words=(("甲己丙", [40, 20, 90, 45]), ("丁戊庚", [40, 60, 90, 85])),
            direction="horizontal"):
        text = "\n".join(w for w, _ in words)
        raw = RecognitionResult(text, [{"text": w, "bbox": b, "confidence": 99} for w, b in words],
                                str(self.source), self.project.source_sha256, 1, 400, 200, "synthetic", "chi_tra",
                                None, 0, engine=engine)
        return self.project.add_result(raw, request={"profile": "traditional", "direction": direction, "dpi": 300,
                                                     "engine_root": None, "policy": "local-three-engine-v1"})

    def collect(self, **kwargs):
        return samples.collect_project(self.project, self.store, **kwargs)

    def manifest(self):
        path = self.store / self.project._id / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class CollectTests(Fixture):
    def test_corrected_line_is_cut_from_the_engine_box_with_confirmed_label(self):
        self.add()
        self.project.set_text(1, "甲已丙\n丁戊庚")
        outcome = self.collect()
        self.assertEqual((outcome["added"], outcome["pages_rendered"]), (1, 1))
        [sample] = self.manifest()["samples"]
        self.assertEqual((sample["read"], sample["label"], sample["kind"], sample["grade"]),
                         ("甲己丙", "甲已丙", "corrected", "human"))
        self.assertEqual(sample["bbox"], [36, 16, 94, 49])            # engine box + padding, never a guessed glyph box
        with Image.open(self.store / self.project._id / sample["file"]) as crop:
            self.assertEqual((crop.size, crop.mode), ((58, 33), "L"))
            self.assertEqual(crop.getpixel((29, 16)), 0)               # the inked line itself

    def test_collecting_again_renders_nothing_and_undo_removes_the_sample(self):
        self.add()
        self.project.set_text(1, "甲已丙\n丁戊庚")
        self.collect()
        again = self.collect()
        self.assertEqual((again["added"], again["kept"], again["pages_rendered"]), (0, 1, 0))
        self.project.undo_edit(1)
        removed = self.collect()
        self.assertEqual(removed["removed"], 1)
        self.assertFalse((self.store / self.project._id).exists())

    def test_reviewed_page_adds_a_few_verified_lines(self):
        self.add()
        self.project.set_reviewed(1, True)
        self.collect()
        kinds = [(s["label"], s["kind"], s["grade"]) for s in self.manifest()["samples"]]
        self.assertEqual(kinds, [("甲己丙", "verified", "human"), ("丁戊庚", "verified", "human")])

    def test_unreviewed_unchanged_lines_are_not_samples(self):
        self.add()
        self.project.set_text(1, "甲已丙\n丁戊庚")
        self.collect()
        self.assertEqual([s["label"] for s in self.manifest()["samples"]], ["甲已丙"])

    def test_edit_in_one_line_keeps_the_next_line_boundary(self):
        self.add()
        self.project.set_text(1, "甲已乙丙\n丁戊辛")
        self.collect()
        self.assertEqual(sorted(s["label"] for s in self.manifest()["samples"]), ["丁戊辛", "甲已乙丙"])

    def test_editorial_changes_are_not_visual_truth(self):
        for edited in ("甲己丙\n丁戊庚。", "甲己丙\n丁戊庚".replace("丁", "丁，")):
            with self.subTest(edited=edited):
                self.setUp()
                self.add()
                self.project.set_text(1, edited)
                self.collect()
                self.assertIsNone(self.manifest())
        self.setUp()
        self.add(words=(("会己", [40, 20, 90, 45]),))
        self.project.set_text(1, "會己")                                 # simplified → traditional conversion
        self.collect()
        self.assertIsNone(self.manifest())

    def test_page_sized_box_has_no_line_position(self):
        self.add(words=(("甲己丙", [0, 0, 400, 200]),))
        self.project.set_text(1, "甲已丙")
        self.collect()
        self.assertIsNone(self.manifest())

    def test_automatic_acceptance_is_weakest_until_a_person_reviews(self):
        self.add(words=(("甲己丙", [40, 20, 90, 45]),))
        raw = RecognitionResult("甲已丙", [], str(self.source), self.project.source_sha256, 1, 400, 200,
                                "synthetic", "chi_tra", 6, 0, engine="tesseract")
        self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                              "engine_root": None, "policy": "local-three-engine-v1"})
        self.project.choose_baseline(1, "ppocrv6")
        suggestion = self.project.compare_page(1)[0]
        self.project.accept_candidate(1, suggestion["id"], "tesseract", actor="auto")
        self.collect()
        self.assertEqual(self.manifest()["samples"][0]["grade"], "ai_auto")
        self.project.set_reviewed(1, True)
        again = self.collect()
        self.assertEqual((again["added"], self.manifest()["samples"][0]["grade"]), (0, "ai_confirmed"))

    def test_disabled_collection_and_changed_source(self):
        self.add()
        self.project.set_text(1, "甲已丙\n丁戊庚")
        samples.set_enabled(False, self.store)
        self.assertEqual(self.collect()["added"], 0)
        samples.set_enabled(True, self.store)
        with Image.new("RGB", (400, 200), "red") as other:
            other.save(self.source)
        outcome = self.collect()
        self.assertEqual(outcome["added"], 0)
        self.assertIn("원본", outcome["warning"])

    def test_stop_request_keeps_what_was_done(self):
        self.add()
        self.project.set_text(1, "甲已丙\n丁戊庚")
        self.assertEqual(self.collect(should_stop=lambda: True)["added"], 0)
        self.assertEqual(self.collect()["added"], 1)

    def test_default_root_follows_environment(self):
        self.assertEqual(samples.samples_root(), Path(samples.os.environ[samples.ENV_PATH]))


class ExportTests(Fixture):
    def setUp(self):
        super().setUp()
        self.add(direction="vertical")
        self.project.set_text(1, "甲已丙\n丁戊庚")
        self.project.set_reviewed(1, True)
        self.collect()

    def test_paddle_recognition_format_with_grade_and_kind_filters(self):
        out = self.root / "export"
        done = samples.export(out, self.store)
        self.assertEqual(done["exported"], 2)
        lines = (out / "rec_gt.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sorted(line.split("\t")[1] for line in lines), ["丁戊庚", "甲已丙"])
        for line in lines:
            self.assertTrue((out / line.split("\t")[0]).is_file())
        meta = [json.loads(line) for line in (out / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({m["document"] for m in meta}, {"synthetic.png"})
        only = samples.export(self.root / "corrected", self.store, kinds=("corrected",))
        self.assertEqual(only["exported"], 1)
        self.assertEqual(samples.export(self.root / "other", self.store, engines=["tesseract"])["exported"], 0)

    def test_rotate_vertical_lines(self):
        out = self.root / "rotated"
        samples.export(out, self.store, rotate_vertical=True)
        name = (out / "rec_gt.txt").read_text(encoding="utf-8").splitlines()[0].split("\t")[0]
        with Image.open(out / name) as image:
            self.assertEqual(image.size, (33, 58))

    def test_export_refuses_a_used_folder_and_skips_changed_files(self):
        used = self.root / "used"
        used.mkdir()
        (used / "keep.txt").write_text("x")
        with self.assertRaises(ValueError):
            samples.export(used, self.store)
        sample = self.manifest()["samples"][0]
        (self.store / self.project._id / sample["file"]).write_bytes(b"changed")
        done = samples.export(self.root / "fresh", self.store)
        self.assertEqual((done["exported"], done["skipped"]), (1, 1))

    def test_stats_and_reset(self):
        info = samples.stats(self.store)
        self.assertEqual((info["documents"], info["samples"], info["by_kind"]), (1, 2, {"corrected": 1, "verified": 1}))
        self.assertTrue(any("조각 2개" in line for line in samples.summary_lines(info)))
        self.assertEqual(samples.reset(self.store), 1)
        self.assertEqual(samples.stats(self.store)["samples"], 0)


if __name__ == "__main__":
    unittest.main()
