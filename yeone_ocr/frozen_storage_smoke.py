"""Offline storage/resume checks executed by the actual packaged executable."""
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def confusion_probe(folder):
    from PIL import Image
    from ab_ocr.ocr import RecognitionResult
    from .comparison import ComparisonProject
    from .comparison_ai import prepare_plan, ReviewPolicy, AISettings
    from . import confusion_memory as memory
    from .file_lock import exclusive_file_lock
    source = folder / 'ledger-fixture.png'
    Image.new('RGB', (400, 200), 'white').save(source)
    def project():
        p = ComparisonProject.open(source)
        raw = RecognitionResult('甲口口丙', [{'text':'甲口口丙','bbox':[40,20,160,60],'confidence':99}],
            str(source), p.source_sha256, 1, 400, 200, 'ledger-fixture', 'chi_tra', None, 0, engine='ppocrv6')
        p.add_result(raw, request={'profile':'traditional','direction':'horizontal','dpi':300,
                                  'engine_root':None,'policy':'local-three-engine-v1'})
        return p
    path = folder / 'ledger-migration.json'
    old = memory._empty()
    old['documents']['legacy'] = {'at':'2026-11-15','source':'legacy.pdf','pages':1,
        'engines':{'ppocrv6':{'seen':{'己':10},'pairs':[['己','已',{'human':1},[]]]}}}
    memory._write_store(old,path)
    for _ in range(2):
        p = project(); p.set_text(1,'甲回丙')
        before = p.to_dict()
        memory.harvest_project(p,path); memory.harvest_project(p,path)
        assert p.to_dict() == before
    learned = memory.LearnedConfusions.load(path)
    row = next(r for r in learned.rows if r.read=='口口')
    assert row.seen == 2 and row.weight == 2 and row.rate == 1 and row.risky
    assert json.loads(path.with_name(path.name+'.before-verified-spans.json').read_text(encoding='utf-8')) == old
    assert memory.read_store(path)[0]['documents']['legacy'] == old['documents']['legacy']
    p = project(); before = p.to_dict()
    plan = prepare_plan(p,[1],ReviewPolicy(selective=True),settings=AISettings('openai','offline'),confusions=learned)
    assert plan.batches[0]['targets'][0]['before']=='口口'
    assert plan.batches[0]['targets'][0]['learned_confusions'][0]['verified_alternatives']==['回']
    assert p.to_dict()==before
    with exclusive_file_lock(path.with_name('lock-probe')):
        pass
    return {'legacy_preserved':True,'legacy_backup_exact':True,'multi_character_routed':True,
            'verified_occurrences':2,'duplicate_observations':0,'project_history_preserved':True,'os_lock_available':True,'network_calls':0}


def review_gui_probe():
    import tkinter as tk
    from tkinter import ttk
    from . import gui, cli
    root = tk.Tk()
    root.withdraw()
    try:
        launches = []
        app = SimpleNamespace(cfg={'ocr_strategy': 'local-first'},
            v_local_resume=tk.StringVar(root, value=gui.RESUME_LABELS['unfinished']),
            v_reader=tk.StringVar(root, value='gemini'),
            _need_pdf=lambda: True, _prepare_env=lambda: True, _save_book=lambda **_: None,
            pdf=lambda: 'synthetic.pdf', work=lambda: Path('synthetic-work'), _pages=lambda: [],
            _read_opts=lambda: ['--reader', 'gemini'], _glyph_opts=lambda **_: [], _answered_opts=lambda: [],
            nb=SimpleNamespace(select=lambda _: None), _with_plan=lambda kind, go: go([]),
            start=lambda args, label, **options: launches.append((args, label, options)))
        button = ttk.Button(root, text=gui.REVIEW_ONLY_TEXT, command=lambda: gui.App.do_run_pdf(app, review_only=True))
        with patch.object(gui.App, '_result_editors_ready', return_value=True), patch.object(gui.App, '_save_ledger'):
            button.invoke()
        (args, label, options), = launches
        assert label == gui.REVIEW_ONLY_TEXT and options['open_after'] is None and '--out' not in args
        parsed = cli.build_parser().parse_args(args)
        assert parsed.local_review_only and parsed.local_ai_review
        return {'choice': label, 'gui_to_cli': True, 'network_calls': 0}
    finally:
        root.destroy()


def storage_probe(folder: Path):
    import pymupdf
    from ab_ocr import ocr
    from .comparison import ComparisonProject
    from . import comparison_storage

    source = folder / "legacy-book.pdf"
    with pymupdf.open() as document:
        for _ in range(64):
            document.new_page(width=400, height=200)
        document.save(source)
    project = ComparisonProject.open(source)
    literal = "神 神 漢，漢\ufe00 한글"
    for number in (1, 2):
        raw = ocr.RecognitionResult(literal, [], str(source), project.source_sha256,
            number, 400, 200, "offline-storage-fixture", "chi_tra", 6, 0, engine="tesseract")
        project.add_result(raw)
        project.set_text(number, literal + " 人校", reason="offline manual correction")
        project.set_reviewed(number)
    expected = project.to_dict()
    # The v1 JSON layout and gzip wrapper used by releases through 1.2.17.
    legacy = project._encoded()
    migrated = []
    for name, data in (("plain", legacy), ("gzip", gzip.compress(legacy))):
        target = folder / (name + ".yeone-compare.json")
        target.write_bytes(data)
        reopened = ComparisonProject.load(target)
        assert reopened.to_dict() == expected
        reopened.save()
        assert comparison_storage.is_database(target)
        assert ComparisonProject.load(target).to_dict() == expected
        reopened.set_text(2, literal + " 二次校正")
        with patch.object(comparison_storage, "_write_rows", wraps=comparison_storage._write_rows) as writer:
            reopened.save()
        assert writer.call_args.args[2] == [2]
        assert ComparisonProject.load(target).to_dict() == reopened.to_dict()
        assert reopened.page(1) == project.page(1)
        recovery = folder / (name + "-recovery.json")
        reopened.save_recovery(recovery)
        assert ComparisonProject.load(recovery).to_dict() == reopened.to_dict()
        migrated.append(name)
    return {"legacy_formats": migrated, "source_pages": 64, "format": "SQLite",
            "complete_history_equal": True, "changed_page_only": True,
            "recovery_equal": True, "network_calls": 0}


def resume_probe(folder: Path):
    from PIL import Image
    from . import cli, comparison, comparison_storage
    from .comparison import ComparisonProject
    from .local_first import command
    from .state import Work

    source = folder / "cancel-resume.png"
    Image.new("RGB", (400, 200), "white").save(source)
    work = folder / "cancel-resume-work"
    calls = []
    cancel_after_response = [True]

    def recognize(project, number, **kwargs):
        added = not project.page(number)["runs"]
        if added:
            text = "天地玄黃\n宇宙洪荒"
            raw = {"kind": "recognition", "schema_version": 1, "engine": "ppocrv6",
                "coordinate_system": "rendered-image pixels, xyxy", "text": text,
                "words": [{"text": "天地玄黃", "bbox": [40, 20, 160, 70], "confidence": 20,
                           "block": 1, "paragraph": 1, "line": 1},
                          {"text": "宇宙洪荒", "bbox": [40, 100, 160, 150], "confidence": 20,
                           "block": 1, "paragraph": 1, "line": 2}],
                "source_path": str(project.source_path), "source_sha256": project.source_sha256,
                "page_number": number, "image_width": 400, "image_height": 200,
                "engine_version": "offline", "languages": "chi_tra", "psm": None,
                "duration_seconds": 0, "preprocessing": {}, "engine_metadata": {}}
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
                "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})
            project.choose_baseline(number, "ppocrv6", automatic=True)
        kwargs["on_result"](None)
        return {"cancelled": False, "completed": ["ppocrv6"] if added else [],
                "skipped": [] if added else ["ppocrv6"], "selection": {"selected_engine": "ppocrv6"}}

    def client(*_args, **_kwargs):
        def create(**kwargs):
            calls.append(1)
            prompt = kwargs["input"][0]["content"][0]["text"]
            data = json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].split("\nReturn JSON", 1)[0])
            result = {"items": [{"target_id": t["id"], "uncertain": False,
                       "note": "offline fixture", "proposals": []} for t in data["targets"]]}
            if cancel_after_response[0]:
                cli.CANCEL.set()
            return SimpleNamespace(output_text=json.dumps(result), status="completed",
                model="offline-vision", id="offline-response",
                usage=SimpleNamespace(input_tokens=12, output_tokens=8))
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)

    def args(review_only=False):
        return cli.build_parser().parse_args(["run", str(source), "--work", str(work),
            "--reader", "openai", "--model", "offline-vision", "--mode", "horizontal_zh", "--pdf-only",
            "--local-ai-auth", "api", "--local-ai-page", "off"] + (["--local-review-only"] if review_only else []))

    cli.CANCEL.clear()
    try:
        import contextlib
        import os
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-fixture-key"}))
            stack.enter_context(patch.object(comparison, "INCREMENTAL_PAGE_COUNT", 1))
            for name in ("make_textlayer", "cmd_build", "cmd_review", "cmd_report"):
                stack.enter_context(patch.object(cli, name, return_value=0))
            stack.enter_context(patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=recognize))
            stack.enter_context(patch("yeone_ocr.comparison_ai.create_client", side_effect=client))
            stack.enter_context(patch("yeone_ocr.subscriptions.pick", side_effect=AssertionError("Subscription forbidden in smoke test")))
            assert command(args()) == 130
            assert len(calls) == 1
            record = Work(str(source), str(work), initialize=False).load_record(1)
            target = Path(record["local_first"]["project_path"])
            assert comparison_storage.is_database(target)
            saved = ComparisonProject.load(target)
            history = saved.page(1)
            assert history["ai_reviews"][0]["status"] == "completed"
            # Manual corrections on another page are covered by storage_probe;
            # here preserve the exact pre-existing derived OCR record as well.
            original_record = record
            cli.CANCEL.clear()
            cancel_after_response[0] = False
            with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=AssertionError("OCR rerun forbidden")), \
                 patch("yeone_ocr.local_first._ai_page_reader", side_effect=AssertionError("Page AI forbidden")), \
                 patch.object(cli, "make_textlayer", side_effect=AssertionError("Output rebuild forbidden")):
                assert command(args(True)) == 0
                assert command(args(True)) == 0
            assert len(calls) == 2
            reopened = ComparisonProject.load(target)
            assert reopened.page(1)["runs"] == history["runs"]
            assert reopened.page(1)["ai_reviews"][:1] == history["ai_reviews"]
            assert len(reopened.page(1)['ai_reviews']) == 2
            record = Work(str(source), str(work), initialize=False).load_record(1)
            for key in ('merged', 'pass1', 'local_grades', 'derived_reading_sha256'):
                assert record[key] == original_record[key]
        return {"cancel_exit": 130, "resume_exit": 0, "synthetic_responses": 2, "remaining_requests_completed": 1,
                "duplicate_requests": 0, "raw_and_ai_history_equal": True,
                "storage": "SQLite", "network_calls": 0, "review_only": True,
                "derived_ocr_preserved": True, "ocr_reruns": 0, "repeat_calls": 0}
    finally:
        cli.CANCEL.clear()
