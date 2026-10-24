"""Synthetic regressions for v6 isolation, verification and app comparison."""
import hashlib
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from ab_ocr import ocr, local_engines, runtime, ppocrv6_worker as worker
from yeone_ocr.comparison import ComparisonProject, ComparisonError, latest_runs, run_page

# These cache migrations compare the four original Chinese engines. The full
# registry also contains optional server/generative engines with other models.
FOUR_ENGINES = ("tesseract", "rapidocr", "easyocr", "ppocrv6")


class PPOCRv6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (100, 100), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.calls = []

    def recognize(self, source, **kwargs):
        self.calls.append(kwargs)
        return ocr.RecognitionResult(text="醫藥臺裏爲", words=[], source_path=str(source),
            source_sha256=self.project.source_sha256, page_number=kwargs["page_number"],
            image_width=100, image_height=100, engine_version="synthetic", languages=kwargs["languages"],
            psm=kwargs["psm"], duration_seconds=0.1, engine=kwargs["engine"])

    def test_four_selected_engines_checkpoint_resume_and_reopen(self):
        checkpoints = []
        with patch.object(ocr, "recognize_file", side_effect=self.recognize):
            result = run_page(self.project, 1, engines=FOUR_ENGINES, on_result=lambda r: checkpoints.append(r.engine))
            resumed = run_page(self.project, 1, engines=FOUR_ENGINES)
        self.assertEqual(result["completed"], list(FOUR_ENGINES))
        self.assertEqual(checkpoints, list(FOUR_ENGINES))
        self.assertEqual(resumed["skipped"], list(FOUR_ENGINES))
        path = self.root / "comparison.json"
        self.project.save(path)
        loaded = ComparisonProject.load(path)
        self.assertEqual(latest_runs(loaded.page(1))["ppocrv6"]["raw"]["text"], "醫藥臺裏爲")

    def test_mixed_profile_never_calls_v6_with_korean_or_changes_old_results(self):
        with patch.object(ocr, "recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, engines=FOUR_ENGINES)
            old_v6 = latest_runs(self.project.page(1))["ppocrv6"]
            outcome = run_page(self.project, 1, engines=FOUR_ENGINES, profile="mixed")
        self.assertIn("ppocrv6", outcome["unsupported"])
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(latest_runs(self.project.page(1))["ppocrv6"], old_v6)
        excluded = self.project.page(1)["comparisons"][-1]["excluded_runs"]
        self.assertIn("ppocrv6", [r["engine"] for r in excluded])

    def test_missing_v6_preserves_existing_engine_results(self):
        def recognize(source, **kwargs):
            if kwargs["engine"] == "ppocrv6":
                raise ocr.OCRError("missing local weights")
            return self.recognize(source, **kwargs)
        with patch.object(ocr, "recognize_file", side_effect=recognize):
            result = run_page(self.project, 1, engines=FOUR_ENGINES)
        self.assertEqual(result["completed"], list(FOUR_ENGINES[:-1]))
        self.assertIn("ppocrv6", result["errors"])

    def test_invalid_selection_rejected_before_recognition(self):
        with patch.object(ocr, "recognize_file") as recognize:
            for engines in ([], ["ppocrv6", "ppocrv6"], ["unknown"]):
                with self.assertRaises(ComparisonError):
                    run_page(self.project, 1, engines=engines)
            with self.assertRaises(ComparisonError):
                run_page(self.project, 1, engines=["ppocrv6"], profile="mixed")
        recognize.assert_not_called()

    def test_language_and_worker_are_independent_from_v5(self):
        self.assertNotIn("kor", local_engines.ENGINE_LANGUAGES["ppocrv6"])
        self.assertEqual(runtime.worker_path("ppocrv6").name, "ppocrv6_worker.py")
        with self.assertRaises(ValueError):
            worker.verify_models(self.root, "kor")

    def make_models(self):
        model_dir = self.root / "models"
        model_dir.mkdir()
        records = []
        for name in (worker.DETECTOR, worker.RECOGNIZER):
            data = ("synthetic " + name).encode()
            (model_dir / name).write_bytes(data)
            records.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"engine": "ppocrv6", "rapidocr_version": "3.10.0", "model_family": "PP-OCRv6",
                    "model_profile": "medium", "models": records}
        (self.root / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
        return model_dir

    def test_worker_rejects_changed_medium_model(self):
        directory = self.make_models()
        self.assertEqual(len(worker.verify_models(directory, "chi_tra")[1]), 2)
        (directory / worker.RECOGNIZER).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            worker.verify_models(directory, "chi_tra")

    def test_worker_rejects_v5_manifest_and_duplicate_records(self):
        directory = self.make_models()
        path = self.root / "installation.json"
        manifest = json.loads(path.read_text())
        manifest["models"].append(manifest["models"][0])
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            worker.verify_models(directory, "chi_sim")
        manifest["engine"] = "rapidocr"
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            worker.verify_models(directory, "chi_sim")

    def test_disabled_classifier_stage_and_variant_text_are_preserved(self):
        engine = lambda _path: SimpleNamespace(boxes=[[[0, 0], [90, 0], [90, 20], [0, 20]]],
                txts=["醫藥臺裏爲"], scores=[0.8], elapse_list=[0.1, None, 0.2])
        instance = worker.Worker(self.root)
        with patch.object(instance, "engine", return_value=(engine, {"model_family": "PP-OCRv6"})), \
                patch.object(worker.importlib.metadata, "version", return_value="3.10.0"):
            result = instance.recognize(self.source, "chi_sim")
        self.assertEqual(result["text"], "醫藥臺裏爲")
        self.assertEqual(result["engine_metadata"]["stage_seconds"], [0.1, None, 0.2])
        self.assertEqual(result["words"][0]["bbox"], [0.0, 0.0, 90.0, 20.0])

    def test_failed_download_preserves_existing_model_and_cleans_partial_file(self):
        from scripts.install_ppocrv6 import download_verified
        path = self.root / "model.onnx"
        path.write_bytes(b"existing bytes")
        response = io.BytesIO(b"wrong downloaded bytes")
        response.url = "https://example.test/model.onnx"
        record = {"file": path.name, "url": response.url, "sha256": hashlib.sha256(b"expected bytes").hexdigest()}
        with patch("scripts.install_ppocrv6.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                download_verified(record, path)
        self.assertEqual(path.read_bytes(), b"existing bytes")
        self.assertFalse(path.with_suffix(".download").exists())

    def test_cli_accepts_explicit_v6_without_initializing_cloud_calls(self):
        from yeone_ocr import cli
        with patch("yeone_ocr.comparison_cli.command", return_value=0) as command:
            self.assertEqual(cli.main(["compare-engines", str(self.source), "--engines", "rapidocr", "ppocrv6"]), 0)
        self.assertEqual(command.call_args.args[0].engines, ["rapidocr", "ppocrv6"])

    def test_cli_relative_source_resumes_project_in_another_directory(self):
        from yeone_ocr.comparison_cli import command
        args = SimpleNamespace(pdf=self.source.name, pages="1", dpi=200,
            project=self.root / "nested/comparison.json", work=self.root / "work",
            profile="traditional", direction="horizontal", engine_root=self.root, force=False,
            engines=["ppocrv6"])
        previous = Path.cwd()
        @contextmanager
        def session(engine, languages, **options):
            yield SimpleNamespace(recognize_file=lambda source, **kwargs: self.recognize(
                source, engine=engine, languages=languages, **kwargs))
        try:
            os.chdir(self.root)
            with patch("ab_ocr.local_engines.RecognitionSession", side_effect=session):
                self.assertEqual(command(args), 0)
                self.assertEqual(command(args), 0)
        finally:
            os.chdir(previous)
        self.assertEqual(len(self.calls), 1)

    def test_v6_model_change_reruns_only_v6(self):
        model = self.root / "tools/ppocrv6/models" / worker.RECOGNIZER
        model.parent.mkdir(parents=True)
        model.write_bytes(b"medium first")
        def recognize(source, **kwargs):
            result = self.recognize(source, **kwargs)
            if kwargs["engine"] == "ppocrv6":
                result.engine_metadata["model_files"] = {model.name: {"sha256": ocr.file_sha256(model)}}
            return result
        with patch.object(ocr, "recognize_file", side_effect=recognize):
            run_page(self.project, 1, engines=FOUR_ENGINES, engine_root=self.root)
            model.write_bytes(b"medium changed")
            outcome = run_page(self.project, 1, engines=FOUR_ENGINES, engine_root=self.root)
        self.assertEqual(outcome["completed"], ["ppocrv6"])
        self.assertEqual(outcome["skipped"], list(FOUR_ENGINES[:-1]))

    def test_easyocr_auxiliary_model_change_invalidates_vertical_cache(self):
        model = self.root / 'tools/easyocr/models/zh_sim_g2.pth'
        model.parent.mkdir(parents=True)
        model.write_bytes(b'auxiliary first')
        def recognize(source, **kwargs):
            result = self.recognize(source, **kwargs)
            if kwargs['engine'] == 'easyocr':
                result.engine_metadata['model_files'] = {model.name: {'sha256':ocr.file_sha256(model)}}
            return result
        with patch.object(ocr,'recognize_file',side_effect=recognize):
            run_page(self.project,1,engines=FOUR_ENGINES,direction='vertical',engine_root=self.root)
            model.write_bytes(b'auxiliary changed')
            outcome=run_page(self.project,1,engines=FOUR_ENGINES,direction='vertical',engine_root=self.root)
        self.assertEqual(outcome['completed'],['easyocr'])
        self.assertEqual(outcome['skipped'],[e for e in FOUR_ENGINES if e!='easyocr'])

    def test_vertical_direction_reaches_local_engines_and_preserves_tesseract_settings(self):
        with patch.object(ocr, "recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, engines=FOUR_ENGINES, direction="vertical", engine_root=self.root)
        calls = {call["engine"]: call for call in self.calls}
        for engine in ("rapidocr", "easyocr", "ppocrv6"):
            self.assertEqual(calls[engine]["direction"], "vertical")
        self.assertEqual(calls["tesseract"]["languages"], "chi_tra_vert")
        self.assertEqual(calls["tesseract"]["psm"], 5)
        self.assertNotIn("direction", calls["tesseract"])

    def check_vertical_cache_migration(self, *, v6_already_sorted, all_already_sorted=False, easyocr_reflow_v1=False, easyocr_dual_v2=False):
        from ab_ocr.reading_order import VERTICAL_RTL_POLICY
        old_request = {"profile": "traditional", "direction": "vertical", "dpi": 200,
                       "engine_root": str(self.root), "policy": "local-three-engine-v1",
                       "installation_sha256": None}
        for engine in FOUR_ENGINES:
            result = self.recognize(self.source, engine=engine, page_number=1,
                                    languages="chi_tra_vert" if engine == "tesseract" else "chi_tra",
                                    psm=5 if engine == "tesseract" else None)
            request = dict(old_request)
            if (engine == "ppocrv6" and v6_already_sorted) or (engine != "tesseract" and all_already_sorted):
                request["reading_order_policy"] = VERTICAL_RTL_POLICY
            if engine == 'easyocr' and easyocr_reflow_v1:
                request['recognition_policy'] = 'easyocr-vertical-unmerged-upright-reflow-v1'
            if engine == 'easyocr' and easyocr_dual_v2:
                request['recognition_policy'] = 'easyocr-vertical-upright-reflow-dual-chinese-v2'
            self.project.add_result(result, request=request)
        old_runs = self.project.page(1)["runs"]
        self.calls.clear()
        with patch.object(ocr, "recognize_file", side_effect=self.recognize):
            outcome = run_page(self.project, 1, engines=FOUR_ENGINES, direction="vertical", dpi=200,
                               engine_root=self.root)
            path = self.root / "vertical-comparison.json"
            self.project.save(path)
            loaded = ComparisonProject.load(path)
            resumed = run_page(loaded, 1, engines=FOUR_ENGINES, direction="vertical", dpi=200,
                               engine_root=self.root)
        expected = (["tesseract", "rapidocr", "easyocr"] if all_already_sorted else
                    ["tesseract", "rapidocr", "easyocr"] + ([] if v6_already_sorted else ["ppocrv6"]))
        self.assertEqual(outcome["completed"], expected)
        self.assertEqual(outcome["skipped"], [e for e in FOUR_ENGINES if e not in expected])
        self.assertEqual([call["engine"] for call in self.calls], expected)
        self.assertEqual(resumed["skipped"], list(FOUR_ENGINES))
        self.assertEqual(loaded.page(1)["runs"][:4], old_runs)
        for engine in ("rapidocr", "easyocr", "ppocrv6"):
            latest = latest_runs(loaded.page(1))[engine]
            self.assertEqual(latest["request"]["reading_order_policy"], VERTICAL_RTL_POLICY)
        from ab_ocr.easyocr_worker import VERTICAL_RECOGNITION_POLICY
        self.assertEqual(latest_runs(loaded.page(1))["easyocr"]["request"]["recognition_policy"],
                         VERTICAL_RECOGNITION_POLICY)

    def test_pre_fix_vertical_cache_reruns_local_engines_and_retains_history(self):
        self.check_vertical_cache_migration(v6_already_sorted=False)

    def test_v6_sorted_cache_reruns_only_v5_and_easyocr_and_retains_history(self):
        self.check_vertical_cache_migration(v6_already_sorted=True)

    def test_sorted_cache_reruns_v5_and_easyocr_for_new_recognition(self):
        self.check_vertical_cache_migration(v6_already_sorted=True, all_already_sorted=True)

    def test_reflow_v1_cache_reruns_v5_and_easyocr_and_retains_history(self):
        self.check_vertical_cache_migration(v6_already_sorted=True,all_already_sorted=True,easyocr_reflow_v1=True)

    def test_dual_v2_cache_reruns_v5_and_easyocr_and_retains_history(self):
        self.check_vertical_cache_migration(v6_already_sorted=True,all_already_sorted=True,easyocr_dual_v2=True)

    def test_installed_font_change_reruns_only_glyph_engines_vertical(self):
        from ab_ocr import glyph_verifier
        first=[{'path':'local.ttf','face_index':0,'sha256':'1'*64}]
        second=[{'path':'local.ttf','face_index':0,'sha256':'2'*64}]
        def recognize(source,**kwargs):
            result=self.recognize(source,**kwargs)
            if kwargs['engine']=='easyocr':
                result.engine_metadata['glyph_font_sources']=glyph_verifier.font_sources()
            return result
        with patch.object(ocr,'recognize_file',side_effect=recognize), \
             patch.object(glyph_verifier,'font_sources',return_value=first) as sources:
            run_page(self.project,1,engines=FOUR_ENGINES,direction='vertical',engine_root=self.root)
            unchanged=run_page(self.project,1,engines=FOUR_ENGINES,direction='vertical',engine_root=self.root)
            self.assertEqual(unchanged['skipped'],list(FOUR_ENGINES))
            sources.return_value=second
            changed=run_page(self.project,1,engines=FOUR_ENGINES,direction='vertical',engine_root=self.root)
        self.assertEqual(changed['completed'],['tesseract','rapidocr','easyocr'])
        self.assertEqual(changed['skipped'],['ppocrv6'])

    def test_legacy_horizontal_cache_reruns_glyph_engines_and_keeps_history(self):
        request={'profile':'traditional','direction':'horizontal','dpi':200,
                 'engine_root':str(self.root),'policy':'local-three-engine-v1','installation_sha256':None}
        for engine in FOUR_ENGINES:
            result=self.recognize(self.source,engine=engine,page_number=1,languages='chi_tra',
                                  psm=6 if engine=='tesseract' else None)
            self.project.add_result(result,request=request)
        previous=self.project.page(1)['runs']
        with patch.object(ocr,'recognize_file',side_effect=self.recognize):
            outcome=run_page(self.project,1,engines=FOUR_ENGINES,dpi=200,engine_root=self.root)
            resumed=run_page(self.project,1,engines=FOUR_ENGINES,dpi=200,engine_root=self.root)
        self.assertEqual(outcome['completed'],list(FOUR_ENGINES))
        self.assertEqual(outcome['skipped'],[])
        self.assertEqual(self.project.page(1)['runs'][:4],previous)
        self.assertEqual(resumed['skipped'],list(FOUR_ENGINES))
        from ab_ocr.easyocr_worker import HORIZONTAL_RECOGNITION_POLICY
        self.assertEqual(latest_runs(self.project.page(1))['easyocr']['request']['recognition_policy'],HORIZONTAL_RECOGNITION_POLICY)

    def test_horizontal_font_change_reruns_glyph_engines_and_mixed_profile_does_not_use_it(self):
        from ab_ocr import glyph_verifier
        first=[{'path':'local.ttf','face_index':0,'sha256':'1'*64}]
        second=[{'path':'local.ttf','face_index':0,'sha256':'2'*64}]
        with patch.object(ocr,'recognize_file',side_effect=self.recognize), \
             patch.object(glyph_verifier,'font_sources',return_value=first) as sources:
            run_page(self.project,1,engines=FOUR_ENGINES,engine_root=self.root)
            sources.return_value=second
            changed=run_page(self.project,1,engines=FOUR_ENGINES,engine_root=self.root)
            self.assertEqual(changed['completed'],list(FOUR_ENGINES))
            self.assertEqual(changed['skipped'],[])
            run_page(self.project,1,engines=('easyocr',),profile='mixed',engine_root=self.root)
        self.assertNotIn('glyph_font_sources',latest_runs(self.project.page(1))['easyocr']['request'])

    def test_previous_native_v5_cache_refreshes_only_v5_after_easyocr_upgrade(self):
        with patch.object(ocr,'recognize_file',side_effect=self.recognize):
            run_page(self.project,1,engines=FOUR_ENGINES,engine_root=self.root)
            legacy=next(r for r in self.project._page(1)['runs'] if r['engine']=='rapidocr')
            legacy['request'].pop('recognition_policy')
            legacy['request'].pop('glyph_font_sources')
            original_raw=legacy['raw']
            outcome=run_page(self.project,1,engines=FOUR_ENGINES,engine_root=self.root)
            resumed=run_page(self.project,1,engines=FOUR_ENGINES,engine_root=self.root)
        self.assertEqual(outcome['completed'],['rapidocr'])
        self.assertEqual(outcome['skipped'],['tesseract','easyocr','ppocrv6'])
        self.assertEqual(resumed['skipped'],list(FOUR_ENGINES))
        self.assertEqual(self.project.page(1)['runs'][1]['raw'],original_raw)


if __name__ == "__main__":
    unittest.main()
