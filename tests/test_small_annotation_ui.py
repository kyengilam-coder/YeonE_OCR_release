"""Offline checks of the explicit-insertion boundary and Tk worker bridge."""
import copy
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from yeone_ocr.small_annotation_ui import (
    AnnotationWorker, ColumnTarget, SmallAnnotationDialog, column_targets,
    annotation_parts, insertion_request, insertion_text, offset_from_prefix, subcolumn_texts,
)


def bundle():
    return {"pages": [{"pdf_page": 7, "record": {"merged": {"regions": [
        {"id": "body", "paragraphs": [{"columns": [
            {"col_from_right": 9, "text": "甲\U00020000漢\ufe00乙"},
            {"col_from_right": 2, "text": "樂한"}]}]}]}},
        "candidates": [{"id": "note-1", "status": "read", "combined_text": "右神左\U000e0100",
                        "subcolumns": [{"side": "left", "text": "左\U000e0100"},
                                       {"side": "right", "text": "右神"}]}]}]}


def horizontal_bundle(count=5):
    value = bundle()
    candidate = value["pages"][0]["candidates"][0]
    candidate.update(direction="horizontal", subcolumns=[
        {"side": f"line-{index}", "text": f"第{index}行神"} for index in range(1, count + 1)])
    candidate["combined_text"] = "".join(part["text"] for part in candidate["subcolumns"])
    return value


class InsertionBoundary(unittest.TestCase):
    def test_saved_column_indices_and_unicode_are_preserved(self):
        value = bundle()
        before = copy.deepcopy(value)
        targets = column_targets(value["pages"][0]["record"])
        self.assertEqual([(t.column_index, t.text) for t in targets], [(0, "甲\U00020000漢\ufe00乙"), (1, "樂한")])
        request = insertion_request(7, value["pages"][0]["candidates"][0], targets[0], 4)
        self.assertEqual(request, {"page": 7, "candidate_id": "note-1", "region_id": "body",
            "paragraph_index": 0, "column_index": 0, "offset": 4, "expected_text": "甲\U00020000漢\ufe00乙", "actor": "human"})
        self.assertEqual(value, before)
        self.assertEqual(column_targets(None), [])

    def test_no_implicit_target_and_no_duplicate_or_partial_insertion(self):
        candidate = bundle()["pages"][0]["candidates"][0]
        target = ColumnTarget("r", 0, 0, "甲乙", "r")
        for chosen, offset in ((None, 0), (target, None), (target, -1), (target, 3), (target, True)):
            with self.subTest(target=chosen, offset=offset), self.assertRaises(ValueError):
                insertion_request(7, candidate, chosen, offset)
        for status in ("detected", "failed", "applied", "already_present", "partial_present", "position_unverified"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                insertion_request(7, {**candidate, "status": status}, target, 0)
        self.assertEqual(insertion_request(7, candidate, target, 0)["offset"], 0)

    def test_caret_uses_codepoints_and_subcolumn_order_uses_side(self):
        self.assertEqual(offset_from_prefix("甲\U00020000漢\ufe00乙", "甲\U00020000漢\ufe00"), 4)
        with self.assertRaises(ValueError):
            offset_from_prefix("甲乙", "wrong")
        self.assertEqual(subcolumn_texts(bundle()["pages"][0]["candidates"][0]), ("右神", "左\U000e0100"))

    def test_partial_requires_explicit_side_and_regular_candidate_rejects_one_side(self):
        candidate = bundle()["pages"][0]["candidates"][0]
        target = ColumnTarget("r", 0, 0, "甲右神乙", "r")
        partial = {**candidate, "status": "partial_present"}
        for side in ("both", "", "unknown"):
            with self.subTest(side=side), self.assertRaises(ValueError):
                insertion_request(7, partial, target, 3, side)
        request = insertion_request(7, partial, target, 3, "left")
        self.assertEqual(request["side"], "left")
        self.assertEqual(request["expected_text"], target.text)
        self.assertEqual(insertion_text(partial, "left"), "左\U000e0100")
        self.assertEqual(insertion_text(partial, "right"), "右神")
        for status in ("read", "uncertain"):
            for side in ("left", "right"):
                with self.subTest(status=status, side=side), self.assertRaises(ValueError):
                    insertion_request(7, {**candidate, "status": status}, target, 3, side)

    def test_horizontal_parts_keep_all_rows_and_require_actual_side_id(self):
        candidate = horizontal_bundle(12)["pages"][0]["candidates"][0]
        candidate["subcolumns"].reverse()
        parts = annotation_parts(candidate)
        self.assertEqual([side for side, _, _ in parts], [f"line-{i}" for i in range(1, 13)])
        self.assertEqual(parts[4], ("line-5", "5행", "第5行神"))
        target = ColumnTarget("r", 0, 0, "甲乙", "r")
        with self.assertRaises(ValueError):
            insertion_request(7, candidate, target, 1, "line-5")
        candidate["status"] = "partial_present"
        self.assertEqual(insertion_request(7, candidate, target, 1, "line-5")["side"], "line-5")
        self.assertEqual(insertion_text(candidate, "line-5"), "第5行神")
        for side in ("both", "right", "line-0", "line-13", ""):
            with self.subTest(side=side), self.assertRaises(ValueError):
                insertion_text(candidate, side)


class WorkerBoundary(unittest.TestCase):
    def wait(self, worker):
        worker.thread.join(timeout=3)
        self.assertFalse(worker.thread.is_alive())
        return worker.take_result()

    def test_call_is_off_ui_thread_and_duplicate_start_is_rejected(self):
        creator = threading.get_ident()
        worker = AnnotationWorker()
        worker.start("read", lambda stop: threading.get_ident())
        with self.assertRaises(RuntimeError):
            worker.start("read", lambda stop: None)
        operation, result, error = self.wait(worker)
        self.assertEqual(operation, "read")
        self.assertNotEqual(result, creator)
        self.assertEqual(error, "")
        self.assertFalse(worker.running)

    def test_cancel_is_observable_and_errors_redact_secret(self):
        ready = threading.Event()
        worker = AnnotationWorker()

        def cancellable(stop):
            ready.set()
            worker.stop.wait(timeout=3)
            return stop()
        worker.start("scan", cancellable)
        self.assertTrue(ready.wait(timeout=3))
        worker.stop.set()
        self.assertTrue(self.wait(worker)[1])

        def fail(stop):
            raise ValueError("configuration contained private-key-fixture")
        worker.start("read", fail, secrets=("private-key-fixture",))
        result = self.wait(worker)
        self.assertNotIn("private-key-fixture", result[2])
        self.assertIn("[REDACTED]", result[2])


class FakeService:
    def __init__(self):
        self.calls = []
        self.saved = bundle()

    def load_bundle(self, work_dir):
        self.calls.append(("load", threading.get_ident(), {}))
        return copy.deepcopy(self.saved)

    def scan_work(self, pdf, work_dir, **kwargs):
        self.calls.append(("scan", threading.get_ident(), kwargs))
        return copy.deepcopy(self.saved)

    def reread_work(self, pdf, work_dir, **kwargs):
        self.calls.append(("read", threading.get_ident(), kwargs))
        return copy.deepcopy(self.saved)

    def apply_candidate(self, pdf, work_dir, **kwargs):
        self.calls.append(("apply", threading.get_ident(), kwargs))
        inserted = insertion_text(self.saved["pages"][0]["candidates"][0], kwargs.get("side", "both"))
        self.saved["pages"][0]["candidates"][0]["status"] = "applied"
        return {"inserted_text": inserted}


class DialogBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        cls.root = None
        gc.collect()

    def setUp(self):
        self.service = FakeService()
        self.dialog = SmallAnnotationDialog(self.root, pdf="fixture.pdf", work_dir=".", service=self.service,
            settings=lambda: {"provider": "gemini", "model": "fixture-model", "api_key": "fixture-key"})
        self.addCleanup(self.cleanup)
        self.wait_idle()

    def cleanup(self):
        if self.dialog.winfo_exists():
            self.dialog.jobs.stop.set()
            if self.dialog.jobs.thread:
                self.dialog.jobs.thread.join(timeout=3)
            self.dialog._destroy()
        self.dialog = None
        gc.collect()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.dialog.jobs.running and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.dialog.jobs.running)

    def choose(self):
        self.dialog.tree.selection_set("0")
        self.dialog._select_candidate()
        self.dialog.target_combo.current(0)
        self.dialog._select_column()

    def test_open_only_loads_and_body_stays_readonly_until_explicit_insertion(self):
        self.assertEqual([c[0] for c in self.service.calls], ["load"])
        self.choose()
        self.assertIsNone(self.dialog.offset)
        self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
        original = self.dialog.target.text
        self.dialog.original.insert("1.0", "overwrite")
        self.assertEqual(self.dialog.original.get("1.0", "end-1c"), original)
        # Tk may count supplementary CJK as two Tcl chars; the service needs one code point.
        self.dialog.original.mark_set("insert", self.dialog.original.search("乙", "1.0"))
        self.dialog._capture_caret()
        self.assertEqual(self.dialog.offset, 4)
        self.assertEqual(str(self.dialog.apply_button["state"]), "normal")
        self.assertFalse(any(call[0] == "apply" for call in self.service.calls))
        self.dialog._apply()
        self.wait_idle()
        call = next(c for c in self.service.calls if c[0] == "apply")
        self.assertNotEqual(call[1], threading.get_ident())
        self.assertEqual((call[2]["offset"], call[2]["expected_text"]), (4, original))
        self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")

    def test_model_read_is_explicit_selected_and_capped(self):
        self.choose()
        self.dialog.v_limit.set("0")
        with patch("yeone_ocr.small_annotation_ui.messagebox.showerror"):
            self.dialog._read()
        self.assertFalse(any(call[0] == "read" for call in self.service.calls))
        self.dialog.v_limit.set("3")
        self.dialog._read()
        self.wait_idle()
        call = next(c for c in self.service.calls if c[0] == "read")
        self.assertNotEqual(call[1], threading.get_ident())
        self.assertEqual((call[2]["pages"], call[2]["candidate_ids"], call[2]["max_calls"]), ([7], ["note-1"], 3))
        self.assertEqual((call[2]["provider"], call[2]["model"]), ("gemini", "fixture-model"))
        self.assertTrue(callable(call[2]["should_stop"]))

    def test_scan_direction_is_explicit_and_defaults_to_automatic(self):
        self.assertEqual(self.dialog.v_direction.get(), "자동")
        for label, expected in (("자동", None), ("가로", True), ("세로", False)):
            with self.subTest(direction=label):
                self.dialog.v_direction.set(label)
                self.dialog._scan()
                self.wait_idle()
                call = [c for c in self.service.calls if c[0] == "scan"][-1]
                self.assertIs(call[2]["horizontal"], expected)
                self.assertTrue(callable(call[2]["should_stop"]))

    def test_horizontal_two_and_five_rows_are_visible_and_partial_selection_resets(self):
        for count in (2, 5):
            with self.subTest(rows=count):
                value = horizontal_bundle(count)
                value["pages"][0]["candidates"][0]["status"] = "partial_present"
                self.dialog._show_bundle(value)
                self.choose()
                displayed = self.dialog.horizontal_text.get("1.0", "end-1c")
                for index in range(1, count + 1):
                    self.assertIn(f"{index}행 ·", displayed)
                    self.assertIn(f"第{index}行神", displayed)
                self.assertLess(displayed.index("第1行"), displayed.index(f"第{count}行"))
                self.assertEqual(str(self.dialog.horizontal_text["state"]), "disabled")
                self.assertEqual(self.dialog.vertical_frame.winfo_manager(), "")
                self.assertEqual(self.dialog.horizontal_frame.winfo_manager(), "pack")
                self.assertIn(f"가로 · {count}행", self.dialog.v_order.get())
                self.assertIn("여러 행이 누락", self.dialog.v_candidate.get())
                self.assertEqual(tuple(self.dialog.side_combo["values"]), tuple(f"{i}행" for i in range(1, count + 1)))
                self.dialog.original.mark_set("insert", "1.0")
                self.dialog._capture_caret()
                self.assertEqual(self.dialog.v_side.get(), "")
                self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
                self.dialog.side_combo.current(count - 1)
                self.dialog._select_side()
                self.assertEqual(self.dialog._insertion_side(), f"line-{count}")
                self.assertIn(f"⟦第{count}行神⟧", self.dialog.preview.get("1.0", "end-1c"))
        self.dialog._show_bundle(bundle())
        self.choose()
        self.assertEqual(self.dialog.v_side.get(), "")
        self.assertEqual(self.dialog.horizontal_text.get("1.0", "end-1c"), "")
        self.assertEqual(self.dialog.horizontal_frame.winfo_manager(), "")
        self.assertEqual(self.dialog.vertical_frame.winfo_manager(), "pack")
        self.assertEqual(self.dialog.right_text.get("1.0", "end-1c"), "右神")
        self.assertEqual(self.dialog.left_text.get("1.0", "end-1c"), "左\U000e0100")

    def test_unverified_position_allows_reread_but_blocks_insertion(self):
        value = horizontal_bundle()
        value["pages"][0]["candidates"][0]["status"] = "position_unverified"
        self.dialog._show_bundle(value)
        self.choose()
        self.dialog.original.mark_set("insert", "1.0")
        self.dialog._capture_caret()
        self.assertEqual(str(self.dialog.read_button["state"]), "normal")
        self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
        self.assertEqual(self.dialog.side_frame.winfo_manager(), "")
        self.assertIn("기존 판독 위치 미확인", self.dialog.v_candidate.get())
        self.assertIn("직접 교정", self.dialog.v_candidate.get())

    def test_missing_reading_and_unselected_partial_never_enable_insert(self):
        value = bundle()
        value["pages"][0]["record"] = None
        self.dialog._show_bundle(value)
        self.dialog.tree.selection_set("0")
        self.dialog._select_candidate()
        self.assertEqual(self.dialog.targets, [])
        self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
        value = bundle()
        value["pages"][0]["candidates"][0]["status"] = "partial_present"
        self.dialog._show_bundle(value)
        self.choose()
        self.dialog.original.mark_set("insert", "1.0")
        self.dialog._capture_caret()
        self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
        self.assertEqual(self.dialog.v_side.get(), "")
        self.assertIn("누락된 소열", self.dialog.v_candidate.get())
        value["pages"][0]["stale"] = True
        self.dialog._show_bundle(value)
        self.choose()
        self.assertEqual(str(self.dialog.read_button["state"]), "disabled")
        self.assertIn("다시 실행", self.dialog.v_candidate.get())

    def test_actual_service_roundtrip_keeps_record_until_explicit_apply(self):
        self.actual_service_roundtrip(partial=False)

    def test_actual_service_partial_missing_half_inserts_only_explicit_side(self):
        self.actual_service_roundtrip(partial=True)

    def test_actual_service_horizontal_five_rows_inserts_in_top_to_bottom_order(self):
        self.actual_service_roundtrip(partial=False, horizontal=True)

    def test_actual_service_horizontal_partial_inserts_only_missing_fifth_row(self):
        self.actual_service_roundtrip(partial=True, horizontal=True)

    def actual_service_roundtrip(self, *, partial, horizontal=False):
        import pymupdf
        from yeone_ocr import small_annotations
        from yeone_ocr.schema import Column, PageReading, Paragraph, Region, RegionReread
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "fixture.pdf"
            with pymupdf.open() as document:
                document.new_page(width=120, height=180)
                document.save(pdf)
            work = Work(str(pdf), str(Path(directory) / "work"))
            parts = ([(f"line-{index}", text) for index, text in enumerate(("一注", "二注", "三注", "四注", "五注"), 1)]
                     if horizontal else [("right", "右注"), ("left", "左注")])
            combined = "".join(text for _, text in parts)
            inserted = parts[-1][1] if partial else combined
            existing = "甲\U00020000漢\ufe00" + ("".join(text for _, text in parts[:-1]) if partial else "") + "乙"
            reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문",
                paragraphs=[Paragraph(columns=[Column(n=1, t=existing)])])])
            work.save_record(1, {"pdf_page": 1, "mode": "horizontal_zh" if horizontal else "vertical", "pass1": reading.model_dump(),
                                 "merged": reading.model_dump(), "human_edits": [{"reason": "prior human edit"}]})
            before = work.record_path(1).read_bytes()
            detected = {"schema_version": 1, "detector": "fixture-geometry", "candidates": [{
                "id": "note-1", "direction": "horizontal" if horizontal else "vertical",
                "bbox_px": [10, 10, 90, 100], "subcolumns": [
                    {"side": "right", "bbox_px": [50, 10, 90, 100]},
                    {"side": "left", "bbox_px": [10, 10, 50, 100]}]}]}
            if horizontal:
                detected["candidates"][0]["subcolumns"] = [
                    {"side": side, "bbox_px": [10, 10 + index * 18, 90, 28 + index * 18]}
                    for index, (side, _) in enumerate(parts)]
            self.dialog.pdf, self.dialog.work_dir = str(pdf), work.dir
            self.dialog._service = small_annotations
            self.dialog._show_bundle({"pages": []})
            with patch("yeone_ocr.small_annotation_detection.detect_small_annotations", return_value=detected):
                self.dialog._scan()
                self.wait_idle()
            self.assertEqual(work.record_path(1).read_bytes(), before)
            self.choose()

            class Reader:
                calls = 0

                def usage_begin(self):
                    pass

                def usage_end(self):
                    return {"calls": 1}

                def reread_region(self, image, kind, region_id, page):
                    self.calls += 1
                    return RegionReread(columns=[Column(n=1, t=next(text for side, text in parts if region_id.endswith(side)))])
            reader = Reader()
            with patch("yeone_ocr.readers.create_reader", return_value=reader):
                self.dialog._read()
                self.wait_idle()
            self.assertEqual(reader.calls, len(parts))
            self.assertEqual(work.record_path(1).read_bytes(), before)
            self.choose()
            self.dialog._context()
            self.wait_idle()
            popup = next(child for child in self.dialog.winfo_children() if isinstance(child, tk.Toplevel))
            canvas = next(child for child in popup.winfo_children() if isinstance(child, tk.Canvas))
            self.assertTrue(any(canvas.type(item) == "rectangle" for item in canvas.find_all()))
            next(child for child in popup.winfo_children() if child.winfo_class() == "TButton").invoke()
            self.assertEqual(work.record_path(1).read_bytes(), before)
            self.choose()
            self.dialog.original.mark_set("insert", self.dialog.original.search("乙", "1.0"))
            self.dialog._capture_caret()
            if partial:
                self.assertEqual(self.dialog.current[1]["status"], "partial_present")
                self.assertEqual(self.dialog.v_side.get(), "")
                self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
                self.dialog.v_side.set("左")  # Only one of the explicit UI choices is valid.
                self.dialog._select_side()
                self.assertEqual(str(self.dialog.apply_button["state"]), "disabled")
                self.dialog.side_combo.current(len(parts) - 1)
                self.dialog._select_side()
                self.assertEqual(str(self.dialog.apply_button["state"]), "normal")
                self.assertIn("⟦" + inserted + "⟧", self.dialog.preview.get("1.0", "end-1c"))
                self.assertNotIn("⟦" + combined + "⟧", self.dialog.preview.get("1.0", "end-1c"))
                self.assertEqual(work.record_path(1).read_bytes(), before)
            self.dialog._apply()
            self.wait_idle()
            result = json.loads(work.record_path(1).read_bytes())
            self.assertEqual(result["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "甲\U00020000漢\ufe00" + combined + "乙")
            self.assertEqual(result["pass1"], reading.model_dump())
            self.assertEqual(result["human_edits"], [{"reason": "prior human edit"}])
            self.assertEqual(result["small_annotation_history"][0]["actor"], "human")
            self.assertEqual(result["small_annotation_history"][0]["inserted_text"], inserted)
            self.assertEqual(result["small_annotation_history"][0]["selected_side"], parts[-1][0] if partial else "both")
            self.assertIn("⟦" + inserted + "⟧", self.dialog.preview.get("1.0", "end-1c"))
            self.assertIn(before, [path.read_bytes() for path in (work.readings_dir / "prev").glob("p*.json")])
            self.assertIn("문서 만들기", self.dialog.v_status.get())


if __name__ == "__main__":
    unittest.main()
