"""Local-first orchestration with actual projects/plans and fake API transport."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import cli
from yeone_ocr.local_first import command, POLICY
from yeone_ocr.comparison import ComparisonProject
from yeone_ocr.state import Work


def test_automatic_split_uses_weighted_rotation_and_skips_unavailable_account():
    from queue import Queue
    from yeone_ocr.local_first import _AiWorkers

    assigned = Queue()
    sessions = {name: SimpleNamespace(available=True, short=lambda name=name: name)
                for name in ('gemini', 'chatgpt', 'claude')}
    for session in sessions.values():
        session.ready_for_page = lambda split, session=session: session.available
        session.prepare_page = lambda split: None
    crews = [{'plan': name, 'session': session, 'reader': None, 'pages': 1}
             for name, session in sessions.items()]
    def stage(number, _started, _review, _extras, session, _reader, _saved):
        assigned.put((number, session.short()))
    def failed(_number, error):
        assigned.put(error)
    workers = _AiWorkers(crews, stage, failed, lambda _message: None)
    names = []
    try:
        for number in range(1, 10):
            if number == 7:
                sessions['chatgpt'].available = False
            workers.submit(number, 0, {}, {})
            page, name = assigned.get(timeout=5)
            assert page == number
            names.append(name)
    finally:
        workers.finish()
    assert names == ['gemini', 'chatgpt', 'claude', 'gemini', 'chatgpt', 'gemini',
                     'gemini', 'claude', 'gemini']


def test_chatgpt_returns_to_page_distribution_before_local_wait_expires(monkeypatch):
    from queue import Queue
    from threading import Event
    from yeone_ocr import chatgpt_subscription, subscriptions
    from yeone_ocr.local_first import _AiWorkers

    assigned = Queue()
    quota_checked, quota_restored, rejoined = Event(), Event(), Event()

    def read_quota(*, stop):
        available = quota_restored.is_set()
        quota_checked.set()
        return available

    def log(message):
        if "한도 회복 확인" in message:
            rejoined.set()

    monkeypatch.setattr(subscriptions, "_clock", lambda: 0.0)  # The 40-minute wait cannot expire.
    monkeypatch.setattr(subscriptions, "CHATGPT_QUOTA_INTERVAL", 0.05)
    monkeypatch.setattr(subscriptions, "status", lambda plan: {"usable": True})
    monkeypatch.setattr(chatgpt_subscription, "quota_available", read_quota)
    subscriptions.reset_exhausted()
    sessions = {plan: subscriptions.Session(plan, log=log) for plan in subscriptions.PLANS}
    sessions["chatgpt"].plan = "claude"
    subscriptions.mark_exhausted("chatgpt", "limit", 2400, kind="limit")
    workers = _AiWorkers(
        [{"plan": plan, "session": session, "reader": None, "pages": 1} for plan, session in sessions.items()],
        lambda page, _started, _review, _extras, session, _reader, _saved: assigned.put((page, session.plan)),
        lambda _page, error: assigned.put(error), log, watch_chatgpt=True,
    )
    try:
        assert quota_checked.wait(5)
        for page, plan in enumerate(("gemini", "claude"), 1):
            workers.submit(page, 0, {}, {})
            assert assigned.get(timeout=5) == (page, plan)
        assert subscriptions.resting()[0]["minutes"] == 40
        assert not rejoined.is_set()
        quota_restored.set()
        assert rejoined.wait(5)
        assert "chatgpt" not in subscriptions.exhausted()
        for page, plan in enumerate(("gemini", "chatgpt"), 3):
            workers.submit(page, 0, {}, {})
            assert assigned.get(timeout=5) == (page, plan)
        assert sessions["chatgpt"].plan == "chatgpt"
    finally:
        workers.finish()
        subscriptions.reset_exhausted()


class LocalFirstTests(unittest.TestCase):
    def test_page_status_not_overwritten_by_individual_proposal_review(self):
        from yeone_ocr.local_first import _record
        import time
        self.assertEqual(command(self.args()), 0)
        rec = self.record()
        project = ComparisonProject.load(rec['local_first']['project_path'])
        project.page(1)['ai_reviews'].append({'id': 'individual', 'status': 'completed',
                                            'request': {'snapshot_text': self.recognized_text}})
        overall = {'status': 'needs-local-review', 'pending_proposals': 1}
        with patch.object(project, 'ai_proposals', return_value=[{'review_id': 'individual', 'start': 0, 'end': 1}]), \
                patch('yeone_ocr.local_first._review_summary', return_value=([], [])), \
                patch('yeone_ocr.local_first._local_grades', return_value=[]):
            result = _record(project, 1, self.args(), Work(str(self.source), str(self.work), initialize=False),
                             time.perf_counter(), overall,
                             extras={'ai_page': {'text': self.recognized_text, 'mode': 'prefer'}})
        self.assertTrue(result['local_first']['ai_page']['used'])
        self.assertEqual(result['local_first']['review'], overall)

    def test_legacy_overwritten_status_replans_once_without_resending_completed_calls(self):
        self.confidence = 20
        self.assertEqual(command(self.args()), 0)
        rec = self.record()
        project = ComparisonProject.load(rec['local_first']['project_path'])
        rec['local_first']['review'] = project.page(1)['ai_reviews'][0]
        work = Work(str(self.source), str(self.work), initialize=False)
        work.save_record(1, rec)
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        repaired = self.record()
        self.assertNotEqual(repaired['local_first']['review']['status'], 'completed')
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.record(), repaired)
        self.assertEqual(len(self.calls), 1)

    def test_human_review_resume_preserves_record_and_skips_ai_planning(self):
        self.assertEqual(command(self.args()), 0)
        work = Work(str(self.source), str(self.work), initialize=False)
        old = self.record()
        old['local_first']['review'].update(status='needs-local-review', pending_proposals=2,
                                           unlocalized_targets=1)
        work.save_record(1, old)
        events = []
        with patch('yeone_ocr.comparison_ai.prepare_plan', side_effect=AssertionError('must not replan')), \
                patch.object(cli, 'PAGE_EVENT', lambda n, state: events.append((n, state))):
            self.assertEqual(command(self.args()), 4)
        self.assertEqual(self.record(), old)
        self.assertEqual(self.calls, [])
        self.assertEqual(events[-1], (1, 'review_pending'))
        self.assertNotIn((1, 'ai-review'), events)
        with patch('yeone_ocr.comparison_ai.prepare_plan', side_effect=AssertionError('human review is not an AI job')):
            self.assertEqual(command(self.args('--local-review-only')), 4)
        self.assertEqual(self.record(), old)

    def test_human_review_all_mode_explicitly_rechecks(self):
        self.assertEqual(command(self.args()), 0)
        work = Work(str(self.source), str(self.work), initialize=False)
        old = self.record()
        old['local_first']['review'].update(status='needs-local-review', pending_proposals=2)
        work.save_record(1, old)
        from yeone_ocr.comparison_ai import prepare_plan
        with patch('yeone_ocr.comparison_ai.prepare_plan', wraps=prepare_plan) as plan:
            self.assertEqual(command(self.args('--local-resume', 'all')), 0)
        plan.assert_called_once()

    def test_human_review_classifier_excludes_unfinished_requests(self):
        from yeone_ocr.local_first import _human_review_only
        for status in ('pending', 'failed', 'cancelled', 'reviewing'):
            self.assertFalse(_human_review_only({'status': status}))
        for extra in ({'engine_errors': {'glmocr': 'failed'}}, {'deferred_calls': 1},
                      {'outcome': {'failed': 1}}, {'outcome': {'cancelled': True}}):
            self.assertFalse(_human_review_only({'status': 'needs-local-review', **extra}))

    def test_subscription_default_continues_past_old_thousand_call_limit(self):
        from yeone_ocr.local_first import _settings, _review_allowance
        args = self.args('--local-ai-auth', 'subscription')
        self.assertEqual(args.local_ai_subscription_calls, 0)
        settings = _settings(args, args.local_ai_subscription_calls)
        for used in (999, 1000, 1439, 10000):
            with self.subTest(used=used):
                self.assertEqual(_review_allowance(150, settings, 0, used, 80), 100)
        self.assertEqual(_review_allowance(20, settings, 1000, 995, 3), 2)
        self.assertEqual(_review_allowance(20, settings, 1000, 997, 3), 0)

    def test_invalid_subscription_limit_stops_before_ocr(self):
        self.assertEqual(command(self.args('--local-ai-auth', 'subscription',
                                           '--local-ai-subscription-calls', '-1')), 2)
        self.assertEqual(self.local_calls, [])

    def test_subscription_explicit_cap_then_unlimited_resume_preserves_completed_calls(self):
        import pymupdf
        from yeone_ocr import claude_subscription
        self.source = self.root / 'subscription-two.pdf'
        with pymupdf.open() as doc:
            doc.new_page(width=96, height=48)
            doc.new_page(width=96, height=48)
            doc.save(self.source)
        self.confidence = 20
        seen = []

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                data = json.loads(prompt.split('Source DATA (JSON):\n')[1].split('\nReturn JSON')[0])
                seen.append(model_name)
                return json.dumps({'items': [{'target_id': t['id'], 'uncertain': False,
                                             'note': 'ok', 'proposals': []} for t in data['targets']]}), True, 'end_turn', {}, model_name

        args = ('--local-ai-auth', 'subscription', '--local-ai-page', 'off', '--local-ai-detail-tile', '0')
        with patch.object(claude_subscription, 'SubscriptionClient', return_value=FakeCLI()), \
                patch.object(claude_subscription, 'status', return_value={'usable': True, 'plan': 'max', 'reason': None}):
            self.assertEqual(command(self.args(*args, '--local-ai-subscription-calls', '1')), 4)
            self.assertEqual(len(seen), 1)
            first = self.record(1)
            self.assertIn('상한 1회', self.record(2)['local_first']['review']['reason'])
            self.assertEqual(command(self.args(*args)), 0)
            self.assertEqual(len(seen), 2)
            self.assertEqual(self.record(1), first)
            self.assertEqual(command(self.args(*args)), 0)
            self.assertEqual(len(seen), 2)

    def test_memory_pressure_stops_before_new_ocr_or_ai(self):
        with patch('yeone_ocr.automatic_ocr.memory_status', return_value={'available_commit_mb': 512, 'available_physical_mb': 2048}):
            self.assertEqual(command(self.args()), 130)
        self.assertEqual(self.local_calls, [])
        self.assertEqual(self.calls, [])

    def test_storage_diagnostic_survives_recovery_failure(self):
        from yeone_ocr.local_first import _preserve_storage_failure
        from yeone_ocr.comparison_storage import storage_failure
        project = SimpleNamespace(project_path=self.root / 'project.json',
                                  save_recovery=lambda p: (_ for _ in ()).throw(OSError('recovery unavailable')))
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')}):
            _preserve_storage_failure(SimpleNamespace(dir=self.work), project,
                                      storage_failure(project.project_path, OSError('save failed')), lambda m: None)
        detail = next((self.work / 'storage-diagnostics').glob('*.json'))
        data = json.loads(detail.read_text('utf8'))
        self.assertTrue(data['stopped_new_requests'])
        self.assertEqual(data['recovery_error_type'], 'OSError')
        self.assertIn('recovery unavailable', data['recovery_error'])

    def test_storage_failure_stops_before_next_page_or_ai_and_saves_recovery(self):
        import fitz
        from yeone_ocr import comparison_storage as storage
        self.source = self.root / 'two-pages.pdf'
        with fitz.open() as doc:
            doc.new_page(width=400, height=200)
            doc.new_page(width=400, height=200)
            doc.save(self.source)
        original = ComparisonProject.save
        def fail_after_recognition(project, *args, **kwargs):
            if self.local_calls:
                raise storage.storage_failure(project.project_path, OSError('disk full'))
            return original(project, *args, **kwargs)
        with patch.object(ComparisonProject, 'save', fail_after_recognition):
            self.assertEqual(command(self.args()), 3)
        self.assertEqual(self.local_calls, [1])
        self.assertEqual(self.calls, [])
        files = list((self.work / 'storage-diagnostics').glob('*.recovery.yeone-compare.json'))
        self.assertEqual(len(files), 1)
        recovery = ComparisonProject.load(files[0])
        self.assertEqual(recovery.page(1)['runs'][0]['raw']['text'], self.recognized_text)

    def test_unreadable_project_stops_before_recognition_and_records_sqlite_code(self):
        import sqlite3
        from yeone_ocr import comparison_storage as storage
        error = sqlite3.DatabaseError('malformed')
        error.sqlite_errorname = 'SQLITE_CORRUPT'
        with patch('yeone_ocr.local_first._project', side_effect=storage.storage_failure('missing.sqlite', error)):
            self.assertEqual(command(self.args()), 3)
        self.assertEqual(self.local_calls, [])
        self.assertEqual(self.calls, [])
        report = next((self.work / 'storage-diagnostics').glob('*.json'))
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['detail']['sqlite_errorname'], 'SQLITE_CORRUPT')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (400, 200), "white").save(self.source)
        self.work = self.root / "work"
        self.confidence = 99
        self.omit_geometry = False
        self.calls = []
        self.local_calls = []
        self.api_failure = False
        self.transport_callback = None
        self.recognized_text = "天地玄黃"
        cli.CANCEL.clear()
        self.addCleanup(cli.CANCEL.clear)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("make_textlayer", "cmd_build", "cmd_review", "cmd_report"):
            mock = patch.object(cli, name, return_value=0)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        p = patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=self.recognize)
        p.start()
        self.addCleanup(p.stop)
        p = patch("yeone_ocr.comparison_ai.create_client", side_effect=self.client)
        p.start()
        self.addCleanup(p.stop)

    def args(self, *extra):
        return cli.build_parser().parse_args(["run", str(self.source), "--work", str(self.work),
            "--reader", "openai", "--model", "synthetic-vision", "--mode", "horizontal_zh",
            "--pdf-only", *extra])

    def recognize(self, project, number, **kwargs):
        self.local_calls.append(number)
        # Reuse completed readings just as the actual engine wrapper does (reported as skipped).
        added = not project.page(number)["runs"]
        if added:
            raw = {"kind": "recognition", "schema_version": 1, "engine": "ppocrv6",
                "coordinate_system": "rendered-image pixels, xyxy", "text": self.recognized_text,
                "words": ([{"text": self.recognized_text, "bbox": [40, 20, 160, 70], "confidence": self.confidence,
                           "block": 1, "paragraph": 1, "line": 1}] if self.recognized_text and not self.omit_geometry else []),
                "source_path": str(project.source_path), "source_sha256": project.source_sha256,
                "page_number": number, "image_width": 400, "image_height": 200,
                "engine_version": "synthetic", "languages": "chi_tra", "psm": None,
                "duration_seconds": 0, "preprocessing": {}, "engine_metadata": {}}
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                             "engine_root": None, "policy": "local-three-engine-v1"})
            project.choose_baseline(number, "ppocrv6", automatic=True)
        kwargs["on_result"](None)
        return {"cancelled": False, "completed": ["ppocrv6"] if added else [], "skipped": [] if added else ["ppocrv6"],
                "selection": {"selected_engine": "ppocrv6"}}

    def client(self, *_args, **_kwargs):
        def create(**kwargs):
            self.calls.append(kwargs)
            if self.api_failure:
                raise OSError("synthetic network failure")
            data = json.loads(kwargs["input"][0]["content"][0]["text"].split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
            result = {"items": [{"target_id": t["id"], "uncertain": False, "note": "verified synthetic",
                "proposals": []} for t in data["targets"]]}
            if self.transport_callback is not None:
                self.transport_callback()
            return SimpleNamespace(output_text=json.dumps(result), status="completed", model="synthetic-vision", id="test",
                                   usage=SimpleNamespace(input_tokens=12, output_tokens=8))
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)

    def record(self, number=1):
        return Work(str(self.source), str(self.work), initialize=False).load_record(number)

    def test_default_run_routes_local_first_and_clean_v6_uses_zero_api(self):
        args = self.args()
        self.assertEqual(args.ocr_strategy, "local-first")
        with patch.object(cli, "cmd_read", side_effect=AssertionError("full-page AI forbidden")):
            self.assertEqual(cli.cmd_run(args), 0)
        self.assertEqual(self.calls, [])
        rec = self.record()
        self.assertEqual(rec["pipeline"], POLICY)
        self.assertEqual(rec["image_frame"], "full-page")
        self.assertEqual(rec["local_first"]["review"]["status"], "no-api-needed")
        self.make_textlayer.assert_called_once()

    def test_low_confidence_single_v6_review_crops_and_resume_skips_paid_call(self):
        self.confidence = 20
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "reviewed")
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        image = project.page(1)["ai_reviews"][0]["request"]["images"][0]
        self.assertNotEqual(image["source_bbox"], [0, 0, 400, 200])
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ComparisonProject.load(project.project_path).page(1)["runs"][0]["raw"]["text"], "天地玄黃")

    def seed_ledger(self, read="地", actual="池"):
        from yeone_ocr import confusion_memory as memory
        store = memory.store_path()
        store.write_text(json.dumps({"format": memory.FORMAT, "version": memory.VERSION, "enabled": True, "suppressed": [],
            "documents": {"e" * 32: {"at": "2026-11-01T00:00:00+0000", "source": "other.pdf", "pages": 3,
                "engines": {"ppocrv6": {"versions": [], "coverage_basis": memory.COVERAGE_BASIS, "seen": {read: 10},
                                        "pairs": [[read, actual, {"human": 3}, []]]}}}}}), encoding="utf-8")
        return memory

    def test_review_only_preserves_reading_and_does_not_reread_or_build(self):
        self.confidence = 20
        self.api_failure = True
        command(self.args())
        before = self.record()
        project = ComparisonProject.load(before['local_first']['project_path'])
        raw_before = project.page(1)['runs']
        self.api_failure = False
        local_before = list(self.local_calls)
        self.make_textlayer.reset_mock()
        with patch('yeone_ocr.local_first._ai_page_reader', side_effect=AssertionError('no page reread')):
            self.assertEqual(command(self.args('--local-review-only')), 0)
        after = self.record()
        for key in before:
            if key not in {'local_first', 'usage'}:
                self.assertEqual(before[key], after[key], key)
        self.assertEqual(self.local_calls, local_before)
        self.make_textlayer.assert_not_called()
        project = ComparisonProject.load(before['local_first']['project_path'])
        self.assertEqual(project.page(1)['runs'], raw_before)
        self.assertEqual(len(project.page(1)['ai_reviews']), 2)
        calls = len(self.calls)
        self.assertEqual(command(self.args('--local-review-only')), 0)
        self.assertEqual(len(self.calls), calls)

    def test_learned_confusion_routes_a_confident_reading_and_the_run_joins_the_ledger(self):
        memory = self.seed_ledger()
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)                       # confidence 99, routed only by the ledger
        prompt = self.calls[0]["input"][0]["content"][0]["text"]
        self.assertIn("LEARNED CONFUSIONS", prompt)
        review = self.record()["local_first"]["review"]
        self.assertEqual(review["difficulty_reasons"], {"learned-confusable": 1})
        self.assertEqual(review["status"], "reviewed")
        project = ComparisonProject.load(self.record()["local_first"]["project_path"])
        self.assertEqual(project.text(1), "天地玄黃")                # the ledger never changes text
        documents = json.loads(memory.store_path().read_text(encoding="utf-8"))["documents"]
        self.assertEqual(documents[project._id]["engines"]["ppocrv6"]["seen"], {"地": 1})

    def test_learned_confusions_can_be_switched_off_for_a_run(self):
        self.seed_ledger()
        self.assertEqual(command(self.args("--no-local-learned-confusions")), 0)
        self.assertEqual(self.calls, [])

    def test_run_end_collects_glyph_samples_for_the_project(self):
        with patch("yeone_ocr.glyph_samples.collect_quietly") as collect:
            self.assertEqual(command(self.args()), 0)
        collect.assert_called_once()
        project = collect.call_args.args[0]
        self.assertEqual(project.project_path, Path(self.record()["local_first"]["project_path"]))

    def test_an_engine_failure_is_reported_and_the_page_is_not_called_checked(self):
        """Codex review (1.2.17): a failed engine left one reading and the page still ended "no API needed"."""
        original = self.recognize
        def recognize(project, number, **kwargs):
            outcome = original(project, number, **kwargs)
            outcome["errors"] = {"glmocr": "GPU 메모리 부족"}
            outcome["selection"] = {"selected_engine": "ppocrv6",
                                    "scores": [{"engine": "ppocrv6", "characters": 4, "score": 0.9}]}
            return outcome
        logs = []
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=recognize), \
                patch.object(cli, "log", side_effect=logs.append):
            self.assertEqual(command(self.args()), 4)                                     # 4: pages left to check
        review = self.record()["local_first"]["review"]
        self.assertEqual(review["status"], "needs-local-review")
        self.assertEqual(review["engine_errors"], {"glmocr": "GPU 메모리 부족"})
        self.assertTrue(any("엔진 실패" in line and "glmocr" in line for line in logs))

    def test_an_engine_failure_on_resume_is_judged_again_not_copied_from_the_last_run(self):
        """Codex re-review: a page finished earlier was reused as "no API needed" though an engine failed now."""
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.record()["local_first"]["review"]["status"], "no-api-needed")
        original = self.recognize
        def recognize(project, number, **kwargs):
            outcome = original(project, number, **kwargs)                       # nothing ran again: reading reused
            outcome["errors"] = {"glmocr": "GPU 메모리 부족"}
            outcome["selection"] = {"selected_engine": "ppocrv6",
                                    "scores": [{"engine": "ppocrv6", "characters": 4, "score": 0.9}]}
            return outcome
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=recognize):
            self.assertEqual(command(self.args()), 4)
        self.assertEqual(self.record()["local_first"]["review"]["status"], "needs-local-review")

    def test_an_engine_failure_with_other_readings_left_still_needs_no_extra_check(self):
        original = self.recognize
        def recognize(project, number, **kwargs):
            outcome = original(project, number, **kwargs)
            outcome["errors"] = {"glmocr": "시간 초과"}
            outcome["selection"] = {"selected_engine": "ppocrv6", "scores": [
                {"engine": "ppocrv6", "characters": 4, "score": 0.9}, {"engine": "surya", "characters": 4, "score": 0.8}]}
            return outcome
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=recognize):
            self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.record()["local_first"]["review"]["status"], "no-api-needed")

    def test_record_carries_per_glyph_grades_for_document_colours(self):
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            command(self.args())
        self.assertEqual(self.record()["local_grades"],
                         [{"region_id": "local-line-1", "para": 0, "start": 0, "end": 4, "grade": "blue"}])

    def test_skip_blank_reads_no_engine_and_saves_an_empty_record(self):
        self.assertEqual(command(self.args("--local-skip-blank")), 0)
        self.assertEqual(self.local_calls, [])
        rec = self.record()
        self.assertEqual((rec["model"], rec["local_first"]["review"]["status"]), ("blank-page", "blank-page"))
        self.assertEqual(rec["merged"]["regions"], [])
        self.assertEqual(self.calls, [])

    def test_options_are_recorded_and_ai_batch_reaches_the_plan(self):
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-batch", "3")), 0)
        rec = self.record()
        self.assertEqual(rec["local_first"]["options"], {"ai_batch": 3})          # columns is the default now
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.page(1)["ai_reviews"][0]["policy"]["batch_size"], 3)

    def test_two_column_order_follows_the_switch(self):
        from yeone_ocr import local_first
        seen = []
        real = local_first._record

        def spy(project, number, args, work, started, review, extras=None):
            seen.append((extras or {}).get("columns"))
            return real(project, number, args, work, started, review, extras)
        with patch.object(local_first, "_record", side_effect=spy):
            command(self.args())
            command(self.args("--no-local-columns", "--force"))
        self.assertTrue(seen[0] is True and seen[-1] is False)
        self.assertEqual(self.record()["local_first"]["options"], {"columns": False})

    def test_easyocr_left_out_of_horizontal_pages_unless_asked(self):
        seen = []
        original = self.recognize

        def capture(project, number, **kwargs):
            seen.append(kwargs.get("exclude", ()))
            return original(project, number, **kwargs)
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=capture):
            command(self.args())
            command(self.args("--local-easyocr", "--force"))
            command(self.args("--local-exclude-engines", "tesseract", "--force"))
        self.assertEqual(seen, [("easyocr",), (), ("easyocr", "tesseract")])

    def test_kept_baseline_from_an_engine_that_did_not_run_this_time_is_used(self):
        """GLM-OCR read the page (GPU on) and an accepted correction pinned that reading; with the GPU engine
        off only PP-OCRv6 runs. The page must go on from the stored GLM-OCR reading, not stop with an error."""
        self.assertEqual(command(self.args()), 0)
        path = next((self.work / "comparison").glob("local-first-*.yeone-compare.json"))
        project = ComparisonProject.load(path, source_path=self.source)
        raw = dict(project.page(1)["runs"][0]["raw"], engine="glmocr", text="天地玄黄",
                   engine_metadata={"generation_complete": True})
        project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                         "engine_root": None, "policy": "local-three-engine-v1"})
        project.choose_baseline(1, "glmocr")
        project.set_text(1, "天地玄黃")
        project.save(path)
        logged = []
        with patch.object(cli, "log", side_effect=logged.append):
            rc = command(self.args("--force"))
        self.assertIn(rc, (0, 4), logged)                                     # 4: done, a page left for a person; 3 would be a failed page
        self.assertTrue(any("고정한 glmocr 판독" in line for line in logged), logged)
        self.assertFalse(any("성공한 로컬 기준 판독이 없습니다" in line for line in logged))
        self.assertEqual(ComparisonProject.load(path, source_path=self.source).page(1)["baseline_engine"], "glmocr")

    def test_missing_baseline_error_says_which_condition_failed(self):
        self.assertEqual(command(self.args()), 0)
        path = next((self.work / "comparison").glob("local-first-*.yeone-compare.json"))
        project = ComparisonProject.load(path, source_path=self.source)
        raw = dict(project.page(1)["runs"][0]["raw"], engine="glmocr", engine_metadata={"generation_complete": True})
        project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                         "engine_root": None, "policy": "local-three-engine-v1"})
        project.choose_baseline(1, "glmocr", automatic=True)                     # not pinned by an edit
        project.page(1)["baseline_choice"] = "automatic"
        project.save(path)
        logged = []
        with patch.object(cli, "log", side_effect=logged.append), \
                patch("yeone_ocr.automatic_ocr.run_automatic_page",
                      return_value={"cancelled": False, "completed": [], "skipped": ["ppocrv6"], "selection": {}}):
            command(self.args("--force"))
        error = next(line for line in logged if "성공한 로컬 기준 판독이 없습니다" in line)
        self.assertIn("glmocr이 이번에 돌지 않았고", error)
        self.assertIn("이번에 읽은 엔진: ppocrv6", error)

    def test_gpu_engines_follow_the_flag_then_the_environment_variable(self):
        seen = []
        original = self.recognize

        def capture(project, number, **kwargs):
            seen.append(kwargs.get("gpu_engines"))
            return original(project, number, **kwargs)
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=capture):
            with patch.dict("os.environ", {"YEONE_OCR_GPU_ENGINES": "1"}):
                command(self.args())
                command(self.args("--no-local-gpu-engines", "--force"))
            with patch.dict("os.environ", {"YEONE_OCR_GPU_ENGINES": "0"}):
                command(self.args("--force"))
                command(self.args("--local-gpu-engines", "--force"))   # 설정에서 켠 것이 환경 변수보다 앞선다
        self.assertEqual(seen, [True, False, False, True])

    def test_subscription_review_runs_through_the_signed_in_cli(self):
        from yeone_ocr import claude_subscription as subscription
        self.confidence = 20
        seen = []
        harness = self

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                harness.calls.append(model_name)
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "end_turn", {"input_tokens": 3}, model_name

        from yeone_ocr import subscriptions
        states = []
        with patch.object(subscriptions, "ON_CHANGE", states.append), \
                patch.object(subscription, "SubscriptionClient", side_effect=lambda **_k: seen.append(1) or FakeCLI()), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-accept",
                                               "--local-ai-page", "off", "--local-ai-batch", "1", "--local-ai-detail-tile", "0")), 0)
        self.assertEqual(seen, [1])                                                    # one CLI client for the run
        self.assertEqual(self.record()["local_first"]["review"]["status"], "reviewed")
        self.assertEqual(self.calls, ["opus"])
        from yeone_ocr import subscriptions
        self.assertIsNone(subscriptions.active())                                     # the window's connection ends with the run
        self.assertEqual((states[0]["plan"], states[-1]), ("claude", None))
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["ai_accept"], {"accepted": 0, "held": 0})
        self.assertEqual(rec["local_first"]["options"], {"ai_accept": True})

    def test_chatgpt_plan_reviews_through_the_codex_cli_with_its_default_model(self):
        from yeone_ocr import chatgpt_subscription, claude_subscription
        self.confidence = 20
        seen = []
        harness = self

        class FakeCodex:
            subscription = True
            transport = chatgpt_subscription.TRANSPORT

            def request(self, *, model_name, system, prompt, images, schema):
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                harness.calls.append(model_name)
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "completed", {"input_tokens": 3}, model_name

        signed_in = {"usable": True, "plan": "ChatGPT", "reason": None, "installed": True}
        with patch.object(chatgpt_subscription, "SubscriptionClient", side_effect=lambda **_k: seen.append("codex") or FakeCodex()), \
                patch.object(claude_subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}), \
                patch.object(chatgpt_subscription, "status", return_value=signed_in):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-plan", "chatgpt",
                                               "--local-ai-page", "off", "--local-ai-batch", "1", "--local-ai-detail-tile", "0")), 0)
        self.assertEqual(seen, ["codex"])                                              # Claude is signed in too
        self.assertEqual(self.calls, ["codex-default"])

    def test_subscription_mode_without_sign_in_keeps_the_review_pending_when_api_fallback_is_off(self):
        from yeone_ocr import claude_subscription as subscription
        self.confidence = 20
        with patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):  # OpenAI key present
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--no-local-ai-api-fallback")), 4)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")

    def test_subscription_mode_without_sign_in_never_uses_a_key_even_with_legacy_flag(self):
        from yeone_ocr import claude_subscription as subscription
        self.confidence = 20
        with patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off",
                                               "--local-ai-api-fallback")), 4)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")

    def _limited(self, seen):
        from yeone_ocr.claude_subscription import SubscriptionError

        class Limited:
            subscription = True

            def request(self, **kwargs):
                seen.append("claude")
                raise SubscriptionError("Claude 구독 사용량 한도(5시간 또는 주간)에 도달했습니다.")
        return Limited()

    def test_plan_at_its_usage_limit_hands_the_same_request_to_the_next_signed_in_plan(self):
        from yeone_ocr import chatgpt_subscription, claude_subscription
        self.confidence = 20
        seen, logs = [], []
        harness = self

        class Codex:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                seen.append(model_name)
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                harness.calls.append(model_name)
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "completed", {}, model_name
        signed_in = {"usable": True, "plan": "p", "reason": None, "installed": True}
        from yeone_ocr import subscriptions
        with patch.object(subscriptions, "PLANS", ("claude", "chatgpt", "gemini")), \
                patch.object(claude_subscription, "SubscriptionClient", return_value=self._limited(seen)), \
                patch.object(chatgpt_subscription, "SubscriptionClient", return_value=Codex()), \
                patch.object(claude_subscription, "status", return_value=signed_in), \
                patch.object(chatgpt_subscription, "status", return_value=signed_in), \
                patch.object(cli, "log", side_effect=lambda message, *a, **k: logs.append(str(message))):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off",
                                               "--local-ai-batch", "1", "--local-ai-detail-tile", "0")), 0)
        self.assertEqual(seen, ["claude", "codex-default"])                            # same request, next plan's model
        self.assertEqual(self.record()["local_first"]["review"]["status"], "reviewed")
        self.assertTrue(any("ChatGPT 구독으로 넘어갑니다" in line for line in logs))

    def test_last_plan_at_limit_preserves_pending_review_and_never_uses_present_api_key(self):
        from yeone_ocr import claude_subscription
        self.confidence = 20
        seen = []
        signed_in = {"usable": True, "plan": "max", "reason": None, "installed": True}
        with patch.object(claude_subscription, "SubscriptionClient", return_value=self._limited(seen)), \
                patch.object(claude_subscription, "status", return_value=signed_in):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off",
                                               "--local-ai-api-fallback", "--local-ai-batch", "1", "--local-ai-detail-tile", "0")), 4)
        self.assertEqual((seen, self.calls), (["claude"], []))
        self.assertEqual(self.record()["local_first"]["review"]["status"], "failed")

    def test_ai_page_reading_goes_into_the_document_not_the_engine_reading(self):
        from yeone_ocr import ai_page, claude_subscription as subscription
        self.recognized_text = "天地玄黄宇宙洪荒"
        sent = []

        class FakeCLI:
            subscription = True

            def request(self, **kwargs):
                sent.append(kwargs)
                return json.dumps({"lines": ["天地玄黃宇宙洪荒"], "uncertain": []}), True, "end_turn", {"input_tokens": 100, "output_tokens": 50}, "opus"
        with patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}), \
                patch.object(subscription, "SubscriptionClient", return_value=FakeCLI()):
            self.assertEqual(command(self.args("--local-ai-page", "prefer", "--no-local-ai-review")), 0)
            initial = self.record()
            self.assertEqual(initial["usage"], {"calls": 1, "prompt_tokens": 100, "output_tokens": 50, "thinking_tokens": 0})
            self.assertFalse(initial["local_first"]["ai_page"]["cached"])
            self.assertEqual(cli.LOCAL_PAGE_CALLS, 1)
            self.assertEqual(command(self.args("--local-ai-page", "prefer", "--no-local-ai-review", "--local-resume", "all")), 0)
            self.assertEqual(cli.LOCAL_PAGE_CALLS, 0)
        rec = self.record()
        self.assertEqual(rec["usage"], initial["usage"])   # cache reuse never adds another stored response
        self.assertTrue(rec["local_first"]["ai_page"]["cached"])
        self.assertEqual(rec["local_first"]["ai_page"]["usage"], rec["usage"])
        from yeone_ocr.report import make_report
        report = make_report(Work(str(self.source), str(self.work), initialize=False), 1)
        self.assertIn("호출 1회", report)
        self.assertIn("새 쪽 판독 호출 없음", report)
        self.assertIn("이번 실행의 새 호출 수나 청구 금액이 아닙니다", report)
        self.assertIn("天地玄黃宇宙洪荒", json.dumps(rec["merged"], ensure_ascii=False))
        # 黄 (engine) / 黃 (AI) is one glyph in two scripts: the AI's form goes in, without a mark to check
        self.assertEqual((rec["local_first"]["ai_page"]["script_taken"], rec["local_first"]["ai_page"]["taken"]), (1, 0))
        self.assertEqual(rec["local_grades"], [])
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.text(1), "天地玄黄宇宙洪荒")                  # engine reading kept for the editor
        self.assertEqual(len(sent), 1)
        self.assertEqual(rec["local_first"]["options"], {"ai_page": "prefer"})

    def test_page_usage_adds_to_review_even_when_merge_does_not_use_response(self):
        from yeone_ocr import local_first
        project = ComparisonProject.open(self.source)
        self.recognize(project, 1, on_result=lambda _: None)
        reviews = [{"metadata": {"usage": {"input_tokens": 12, "output_tokens": 8}}}]
        args = self.args("--local-ai-page", "prefer")
        for text in (self.recognized_text, "", "甲乙丙丁戊己庚辛"):
            with self.subTest(text=text), patch.object(local_first, "_review_summary", return_value=([], reviews)):
                rec = local_first._record(project, 1, args, None, 0, {"status": "no-api-needed"}, extras={"ai_page": {
                    "text": text, "mode": "prefer", "model": "fake", "cached": False,
                    "usage": {"prompt_token_count": 100, "candidates_token_count": 50, "thoughts_token_count": 3}}})
                self.assertEqual(rec["usage"], {"calls": 2, "prompt_tokens": 112, "output_tokens": 58, "thinking_tokens": 3})
                self.assertEqual(rec["local_first"]["ai_page"]["used"], text == self.recognized_text)
                self.assertEqual(local_first._resume_done(rec, args, rec["local_first"]["resume_key"], True), bool(text))

    def test_response_usage_only_keeps_finite_nonnegative_public_counts(self):
        from yeone_ocr.local_first import _response_usage
        self.assertEqual(_response_usage({"input_tokens": True, "output_tokens": -2,
                                         "thoughts_token_count": float("nan"), "secret": "do-not-store"}),
                         {"calls": 1, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0})
        self.assertEqual(_response_usage(None), {"calls": 1, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0})

    def test_ai_page_without_sign_in_keeps_the_engine_reading(self):
        from yeone_ocr import claude_subscription as subscription
        with patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):
            self.assertEqual(command(self.args("--local-ai-page", "prefer", "--no-local-ai-review")), 0)
        self.assertNotIn("ai_page", self.record()["local_first"])

    def test_signed_in_subscription_turns_on_page_reading_and_tiled_review_by_default(self):
        from yeone_ocr import claude_subscription as subscription, local_first
        args = self.args()
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            resolved = local_first._resolve_ai_defaults(args, cli)
        self.assertEqual((args.local_ai_page, args.local_ai_accept, args.local_ai_detail_tile, args.local_ai_batch,
                          args.local_ai_auth), ("prefer", True, 1024, 4, "subscription"))
        self.assertEqual(resolved["ai_page"], "prefer")
        args = self.args("--local-ai-page", "off")
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            local_first._resolve_ai_defaults(args, cli)
        self.assertEqual(args.local_ai_page, "off")                                   # explicit choice wins
        args = self.args()
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):
            local_first._resolve_ai_defaults(args, cli)
        self.assertEqual((args.local_ai_page, args.local_ai_accept, args.local_ai_batch), ("off", False, 1))
        self.assertIsNone(args.local_ai_auth)

    def test_finished_page_is_passed_over_on_the_next_run_without_cropping_planning_or_writing(self):
        from yeone_ocr import local_first
        self.confidence = 20
        self.assertEqual(command(self.args()), 0)                                       # reviewed through the API
        self.assertEqual(len(self.calls), 1)
        record = self.record()
        self.assertEqual(record["local_first"]["review"]["status"], "reviewed")
        logs = []
        forbidden = AssertionError("끝난 쪽을 다시 계획함")
        with patch("yeone_ocr.comparison_ai.prepare_plan", side_effect=forbidden), \
                patch.object(local_first, "_save_record", side_effect=AssertionError("끝난 쪽 기록을 다시 씀")), \
                patch.object(cli, "log", side_effect=lambda message, *a, **k: logs.append(str(message))):
            self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)                                            # no new call
        self.assertTrue(any("1쪽: 끝난 쪽 — 같은 조건이라 건너뜀 · AI 검증 끝" in line for line in logs))
        self.assertEqual(self.record()["read_at"], record["read_at"])                   # the record is left as it was

    def test_a_changed_page_text_or_setting_or_an_unfinished_review_is_processed_again(self):
        from yeone_ocr import local_first
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(command(self.args()), 4)                                   # review pending: no key
        planned = []
        real = local_first._save_record

        def save(*args, **kwargs):
            planned.append(1)
            return real(*args, **kwargs)
        with patch.object(local_first, "_save_record", side_effect=save):
            self.assertEqual(command(self.args()), 0)                                   # pending → reviewed now
        self.assertTrue(planned); self.assertEqual(self.record()["local_first"]["review"]["status"], "reviewed")
        project = ComparisonProject.load(self.record()["local_first"]["project_path"])
        project.set_text(1, "天地玄黃宇", reason="manual")                                 # a person corrected the page
        project.save()
        planned.clear()
        with patch.object(local_first, "_save_record", side_effect=save):
            command(self.args())
        self.assertTrue(planned)                                                        # text changed: not passed over
        planned.clear()
        with patch.object(local_first, "_save_record", side_effect=save):
            command(self.args("--no-local-columns"))
        self.assertTrue(planned)                                                        # a setting changed: not passed over

    def test_resume_choice_continue_leaves_earlier_pages_unchecked_and_all_checks_them_again(self):
        from yeone_ocr import local_first
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(command(self.args()), 4)                                   # review pending (no key)
        engines = len(self.local_calls)
        logs = []
        with patch("yeone_ocr.comparison_ai.prepare_plan", side_effect=AssertionError("앞선 쪽을 점검함")), \
                patch.object(cli, "log", side_effect=lambda message, *a, **k: logs.append(str(message))):
            self.assertEqual(command(self.args("--local-resume", "continue")), 4)       # still incomplete: left as it was
        self.assertEqual(len(self.local_calls), engines)                                # not even the engine step
        self.assertEqual(self.calls, [])
        self.assertTrue(any("1쪽: 앞선 기록 그대로 — 바로 이어가기(점검 안 함) · AI 검증 보류" in line for line in logs))
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")
        self.assertEqual(command(self.args()), 0)                                       # unfinished (default): sent now
        self.assertEqual(len(self.calls), 1)
        planned = []
        real = local_first._save_record
        with patch.object(local_first, "_save_record", side_effect=lambda *a, **k: planned.append(1) or real(*a, **k)):
            self.assertEqual(command(self.args("--local-resume", "all")), 0)            # finished, but checked again
        self.assertTrue(planned)
        self.assertEqual(len(self.calls), 1)                                            # finished review not bought again

    def test_requests_go_three_at_a_time_through_a_plan_and_one_by_one_through_an_api_key(self):
        from yeone_ocr import claude_subscription, comparison_ai
        self.confidence = 20
        real, seen = comparison_ai.run_plan, []

        def spy(*args, **kwargs):
            seen.append(kwargs.get("parallel"))
            return real(*args, **kwargs)

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "end_turn", {}, model_name
        with patch("yeone_ocr.comparison_ai.run_plan", side_effect=spy), \
                patch.object(claude_subscription, "SubscriptionClient", side_effect=lambda **_k: FakeCLI()), \
                patch.object(claude_subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off")), 0)
            self.work = self.root / "work-api"
            self.assertEqual(command(self.args("--local-ai-auth", "api")), 0)
        self.assertEqual(seen, [3, 1])

    def test_split_hands_pages_to_the_signed_in_plans(self):
        from yeone_ocr import chatgpt_subscription, claude_subscription
        self.confidence = 20
        answered, logs = [], []

        def fake(name):
            class FakeCLI:
                subscription = True

                def request(self, *, model_name, system, prompt, images, schema):
                    answered.append((name, model_name))
                    data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                    return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                                 for t in data["targets"]]}), True, {"claude": "end_turn", "chatgpt": "completed"}[name], {}, model_name
            return FakeCLI()
        signed_in = {"usable": True, "plan": "p", "reason": None, "installed": True}
        from yeone_ocr import subscriptions
        with patch.object(subscriptions, "PLANS", ("claude", "chatgpt", "gemini")), \
                patch.object(claude_subscription, "SubscriptionClient", side_effect=lambda **_k: fake("claude")), \
                patch.object(chatgpt_subscription, "SubscriptionClient", side_effect=lambda **_k: fake("chatgpt")), \
                patch.object(claude_subscription, "status", return_value=signed_in), \
                patch.object(chatgpt_subscription, "status", return_value=signed_in), \
                patch.object(cli, "log", side_effect=lambda message, *a, **k: logs.append(str(message))):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off", "--local-ai-split")), 0)
        self.assertTrue(any("쪽 나눠 보내기: Claude · ChatGPT 구독 · 빈자리 있는 계정끼리 우선순위 비중 2:1" in line for line in logs))
        self.assertTrue(any("1쪽: AI 담당 계정 · Claude 구독" in line for line in logs))
        self.assertEqual(answered, [("claude", "opus")])                               # one page: the first free plan
        self.assertEqual(self.record()["local_first"]["review"]["status"], "reviewed")

    def test_page_reading_and_review_go_out_together(self):
        import threading
        from yeone_ocr import claude_subscription
        self.confidence = 20
        reading, reviewing, both = threading.Event(), threading.Event(), []

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                if "Source DATA (JSON):" in prompt:
                    reviewing.set()
                    both.append(reading.wait(10))                      # the page reading is in flight at the same time
                    data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                    return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                                 for t in data["targets"]]}), True, "end_turn", {}, model_name
                reading.set()
                both.append(reviewing.wait(10))                        # and the review does not wait for it
                return json.dumps({"lines": [self_.recognized_text], "uncertain": []}), True, "end_turn", {}, "opus"
        self_ = self
        with patch.object(claude_subscription, "SubscriptionClient", side_effect=lambda **_k: FakeCLI()), \
                patch.object(claude_subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "prefer")), 0)
        self.assertEqual(both, [True, True])
        record = self.record()["local_first"]
        self.assertEqual(record["review"]["status"], "reviewed")
        self.assertIn("ai_page", record)

    def test_engines_read_on_while_the_ai_part_waits_and_progress_counts_finished_pages(self):
        import threading
        from yeone_ocr import claude_subscription
        self.confidence = 20
        self.source = self.root / "book.pdf"
        pages = [Image.new("RGB", (400, 200), "white") for _ in range(3)]
        pages[0].save(self.source, save_all=True, append_images=pages[1:], resolution=300)     # 400x200 px at the run's 300 DPI
        all_read, waited, progress, events, logs = threading.Event(), [], [], [], []
        recognize = self.recognize

        def engines(project, number, **kwargs):
            out = recognize(project, number, **kwargs)
            if number == 3:
                all_read.set()
            return out

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                waited.append(all_read.wait(10))                       # no answer until the engines have read every page
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "end_turn", {}, model_name
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=engines), \
                patch.object(claude_subscription, "SubscriptionClient", side_effect=lambda **_k: FakeCLI()), \
                patch.object(claude_subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}), \
                patch.object(cli, "PROGRESS", lambda stage, done, total: progress.append((stage, done, total))), \
                patch.object(cli, "PAGE_EVENT", lambda number, state: events.append((number, state))), \
                patch.object(cli, "log", side_effect=lambda message, *a, **k: logs.append(str(message))):
            rc = command(self.args("--local-ai-auth", "subscription", "--local-ai-page", "off", "--local-ai-pages", "1"))
        self.assertEqual(rc, 0)
        self.assertTrue(waited and all(waited))                         # the engines did not wait for the first answer
        self.assertEqual([p for p in progress if p[0] == "read"], [("read", 1, 3), ("read", 2, 3), ("read", 3, 3)])
        self.assertEqual([state for number, state in events if number == 3], ["ocr", "queued", "ai-review", "done"])
        self.assertTrue(any("엔진은 AI의 답을 기다리지 않고" in line for line in logs))
        for number in (1, 2, 3):
            self.assertEqual(self.record(number)["local_first"]["review"]["status"], "reviewed")

    def test_missing_key_saves_local_and_pending_review(self):
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(command(self.args()), 4)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")
        self.make_textlayer.assert_called_once()

    def test_disabled_review_has_no_external_call(self):
        self.confidence = 20
        self.assertEqual(command(self.args("--no-local-ai-review")), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["reason"], "AI 검증 꺼짐")

    def test_api_failure_keeps_raw_and_builds_local_output(self):
        self.confidence = 20
        self.api_failure = True
        self.assertEqual(command(self.args()), 4)
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "failed")
        self.assertEqual(rec["pass1"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "天地玄黃")
        self.make_textlayer.assert_called_once()

    def test_disabled_review_preserves_unlocalized_notice_without_api_error(self):
        self.omit_geometry = True
        self.assertEqual(command(self.args("--no-local-ai-review")), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "needs-local-review")

    def test_legacy_or_manually_edited_reading_is_preserved(self):
        self.assertEqual(command(self.args()), 0)
        old = self.record()
        old["merged"]["page_note"] = "human correction"
        work = Work(str(self.source), str(self.work), initialize=False)
        work.save_record(1, old)
        self.local_calls.clear()
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.local_calls, [])
        self.assertEqual(self.record(), old)

    def test_document_build_disables_extra_context_model(self):
        args = self.args()
        args.pdf_only = False
        with patch.object(cli, "_check_document", return_value=None):
            self.assertEqual(command(args), 0)
        self.assertTrue(self.cmd_build.call_args.args[0].no_context_model)

    def test_total_call_cap_applies_across_pages(self):
        import pymupdf
        self.source = self.root / "two.pdf"
        with pymupdf.open() as document:
            document.new_page(width=96, height=48)
            document.new_page(width=96, height=48)
            document.save(self.source)
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-max-calls", "1")), 4)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.record(2)["local_first"]["review"]["status"], "pending")

    def test_multiframe_review_resume_and_changed_cap_never_repeat_paid_calls(self):
        import pymupdf
        self.source = self.root / "two-resume.pdf"
        with pymupdf.open() as document:
            document.new_page(width=96, height=48)
            document.new_page(width=96, height=48)
            document.save(self.source)
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-max-calls", "3")), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(command(self.args("--local-ai-max-calls", "3")), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(command(self.args("--local-ai-max-calls", "7")), 0)
        self.assertEqual(len(self.calls), 2)
        for number in (1, 2):
            rec = self.record(number)
            project = ComparisonProject.load(rec["local_first"]["project_path"])
            self.assertEqual(len(project.page(number)["ai_reviews"]), 1)

    def test_relative_source_and_work_paths_resume_without_duplicate_review(self):
        original_cwd = Path.cwd()
        original_source, original_work = self.source, self.work
        self.confidence = 20
        try:
            os.chdir(self.root)
            self.source, self.work = Path("source.png"), Path("work")
            self.assertEqual(command(self.args()), 0)
            self.assertEqual(len(self.calls), 1)
            rec = self.record()
            project = ComparisonProject.load(rec["local_first"]["project_path"])
            self.assertEqual(project.source_path, original_source.resolve())
            self.assertEqual(command(self.args()), 0)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(len(ComparisonProject.load(project.project_path).page(1)["ai_reviews"]), 1)
        finally:
            os.chdir(original_cwd)
            self.source, self.work = original_source, original_work

    def test_failed_forced_rerecognition_cannot_certify_stale_baseline(self):
        self.assertEqual(command(self.args()), 0)
        before = (self.work / "readings" / "p001.json").read_bytes()
        outcome = {"cancelled": False, "completed": [], "skipped": [],
                   "errors": {"ppocrv6": "synthetic engine failure"}, "selection": {}}
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", return_value=outcome):
            self.assertNotEqual(command(self.args("--force")), 0)
        self.assertEqual((self.work / "readings" / "p001.json").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_profile_change_failure_preserves_previous_reading(self):
        self.assertEqual(command(self.args()), 0)
        before = (self.work / "readings" / "p001.json").read_bytes()
        outcome = {"cancelled": False, "completed": [], "skipped": [],
                   "errors": {"ppocrv6": "synthetic unsupported model"}, "selection": {}}
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", return_value=outcome):
            self.assertNotEqual(command(self.args("--force", "--local-profile", "simplified")), 0)
        self.assertEqual((self.work / "readings" / "p001.json").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_cancel_after_paid_response_checkpoints_and_resumes_without_resend(self):
        self.confidence = 20
        self.transport_callback = cli.CANCEL.set
        self.assertEqual(command(self.args()), 130)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_not_called()
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "cancelled")
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.page(1)["ai_reviews"][0]["status"], "completed")
        self.assertEqual(project.page(1)["runs"][0]["raw"]["text"], "天地玄黃")
        cli.CANCEL.clear()
        self.transport_callback = None
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_called_once()

    def test_true_blank_local_success_needs_no_api(self):
        self.recognized_text = ""
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.calls, [])
        rec = self.record()
        self.assertEqual(rec["pass1"]["regions"], [])
        self.assertEqual(rec["merged"]["regions"], [])
        self.assertEqual(rec["local_first"]["review"]["status"], "no-api-needed")

    def test_source_change_during_transport_blocks_document_output(self):
        self.confidence = 20
        def replace_source():
            with Image.new("RGB", (400, 200), "black") as changed:
                changed.save(self.source)
        self.transport_callback = replace_source
        self.assertNotEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_not_called()
        self.cmd_build.assert_not_called()
        self.assertEqual(self.record()["pass1"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "天地玄黃")

    def test_review_and_report_display_pending_without_injecting_html(self):
        from yeone_ocr.review_html import make_review
        self.confidence = 20
        command(self.args("--no-local-ai-review"))
        work = Work(str(self.source), str(self.work), initialize=False)
        rec = self.record()
        rec["local_first"]["ai_proposals"] = [{"before": "<script>", "after": "&", "reason": "<bad>", "decision": "pending"}]
        text = Path(make_review(work, [rec])).read_text(encoding="utf-8")
        self.assertIn("로컬 우선", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
