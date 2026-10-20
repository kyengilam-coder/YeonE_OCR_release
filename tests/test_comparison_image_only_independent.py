"""Independent image-only proof, history, pending UI and AI context checks."""
import builtins
import copy
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_comparison_ai as fake_ai
from tests import test_comparison_editor as hosted
from tests import test_comparison_refinement as fixture_module
from yeone_ocr import comparison as core
from yeone_ocr import comparison_image_only as checks
from yeone_ocr import comparison_refinement as refinement
from yeone_ocr.comparison_ai import AISettings, ReviewPolicy, prepare_plan, run_plan


class ImageCheckIndependentTests(unittest.TestCase):
    response = staticmethod(fake_ai.PipelineTests.response)
    prompt_data = staticmethod(fake_ai.PipelineTests.prompt_data)
    client = fake_ai.PipelineTests.client

    def setUp(self):
        self.fixture = fixture_module.RefinementTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project

    def build(self, before='·', after=':'):
        text = '甲乙丙丁' + before + '己庚辛壬'
        self.fixture.text = text
        for engine, content in [('glmocr',text),('easyocr',text.replace(before,after)),
                                ('rapidocr',text),('surya',text)]:
            self.fixture.add(engine,content)
        self.project.choose_baseline(1,'glmocr')
        return refinement.build_candidates(self.project,1)[0]

    def test_legacy_local_hold_reopen_and_all_histories_remain_separate(self):
        item=self.build();before=self.project.page(1)
        record=checks.record_checks(self.project,1)
        self.assertEqual(len(record['verdicts']),1)
        self.assertTrue(record['verdicts'][0]['blocked'])
        view=refinement.proposals(self.project,1)[0]
        self.assertFalse(view['eligible'])
        self.assertEqual(refinement.evaluate_proposals(self.project,1)['text'],self.fixture.text)
        self.assertEqual(refinement.refinement_context(self.project,1)['proposals'],[])
        with self.assertRaises(core.ComparisonError):
            refinement.accept_proposal(self.project,1,item['review_id'],item['id'])
        after=self.project.page(1)
        for key in ('runs','edits','ai_reviews','ai_decisions','refinement_reviews','refinement_decisions'):
            self.assertEqual(before.get(key),after.get(key))
        self.assertEqual(self.fixture.reopen().page(1),after)

    def test_region_variant_uses_same_protection_and_preserves_raw_region(self):
        self.fixture.text='甲乙丙丁卽己庚辛壬'
        for engine in ('glmocr','rapidocr','surya'):
            self.fixture.add(engine,self.fixture.text)
        self.project.choose_baseline(1,'glmocr')
        plan=refinement.plan_region(self.project,1,4,5,'glmocr',anchor_engine='rapidocr')
        refinement.run_region(self.project,plan,session=self.fixture.session(text=self.fixture.text.replace('卽','即')))
        item=refinement.build_region_candidates(self.project,1)[0]
        before=self.project.page(1)
        record=checks.record_checks(self.project,1)
        self.assertIn('protected_source_variant',record['verdicts'][0]['reason_codes'])
        self.assertFalse(refinement.proposals(self.project,1)[0]['eligible'])
        with self.assertRaises(core.ComparisonError):refinement.accept_proposal(self.project,1,item['review_id'],item['id'])
        self.assertEqual(before['region_readings'],self.project.page(1)['region_readings'])
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_idempotent_record_and_returned_value_mutation_never_change_proofs(self):
        item=self.build();first=checks.record_checks(self.project,1)
        original=self.project.page(1)
        second=checks.record_checks(self.project,1)
        self.assertEqual(first,second)
        first['verdicts'][0]['blocked']=False
        view=refinement.proposals(self.project,1)[0]
        view['image_only_check']['blocked']=False
        self.assertEqual(self.project.page(1),original)
        self.assertTrue(checks.evidence_for(original,'refinement',item['review_id'],item['id'])['blocked'])

    def test_later_saved_reading_and_new_check_keep_historical_verdict(self):
        self.build();first=checks.record_checks(self.project,1)
        self.fixture.add('surya',self.fixture.text.replace('·',':'))
        refinement.build_candidates(self.project,1)
        checks.record_checks(self.project,1)
        page=self.project.page(1)
        self.assertEqual(page['image_only_checks'][0],first)
        self.assertEqual(self.fixture.reopen().page(1),page)

    def test_rehashed_false_verdict_is_rejected_on_reopen(self):
        self.build();checks.record_checks(self.project,1)
        path=self.fixture.folder/'tampered.json';self.project.save(path)
        data=json.loads(path.read_text(encoding='utf-8'))
        record=data['pages']['1']['image_only_checks'][0]
        verdict=record['verdicts'][0];verdict['blocked']=False;verdict['reason_codes']=[]
        verdict['verdict_sha256']=core._digest({k:v for k,v in verdict.items() if k!='verdict_sha256'})
        record['id']=core._digest({k:v for k,v in record.items() if k not in ('id','created_at')})
        path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(core.ComparisonError):core.ComparisonProject.load(path)

    def test_forged_candidate_cannot_become_a_different_verified_replacement(self):
        self.build();checks.record_checks(self.project,1)
        path=self.fixture.folder/'forged-proposal.json';self.project.save(path)
        data=json.loads(path.read_text(encoding='utf-8'))
        data['pages']['1']['refinement_reviews'][0]['proposals'][0]['after']='。'
        path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(core.ComparisonError):core.ComparisonProject.load(path)

    def test_old_human_acceptance_is_not_rewritten_or_retroactively_held(self):
        item=self.build()
        # Seed an old human decision directly in the old persisted format;
        # new public acceptance must use current protection, tested below.
        replacement=self.fixture.text[:item['start']]+item['after']+self.fixture.text[item['end']:]
        self.project._set_text(1,replacement,'anchored-local-refinement',kind='refinement_accept',
            extra={'refinement_review_id':item['review_id'],'refinement_proposal_id':item['id'],
                   'applied_start':item['start'],'applied_end':item['end']})
        page=self.project._page(1)
        page.setdefault('refinement_decisions',[]).append({'id':uuid.uuid4().hex,'at':core._now(),
            'review_id':item['review_id'],'proposal_id':item['id'],'decision':'accepted',
            'edit_id':page['edits'][-1]['id']})
        before=copy.deepcopy(self.project.page(1))
        self.assertIsNone(checks.record_checks(self.project,1))
        self.assertEqual(self.fixture.reopen().page(1),before)
        self.project.undo_edit(1)
        after_undo=copy.deepcopy(self.project.page(1))
        checks.record_checks(self.project,1)
        for key in ('runs','edits','refinement_reviews','refinement_decisions'):
            self.assertEqual(self.project.page(1)[key],after_undo[key])
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_old_pending_candidate_is_protected_before_any_explicit_check(self):
        item=self.build();before=self.project.page(1)
        view=refinement.proposals(self.project,1)[0]
        self.assertFalse(view['eligible']);self.assertTrue(view['image_only_current']['blocked'])
        self.assertNotIn('image_only_checks',self.project.page(1))
        with self.assertRaises(core.ComparisonError):refinement.accept_proposal(self.project,1,item['review_id'],item['id'])
        self.assertEqual(self.project.page(1),before)
        checks.record_checks(self.project,1)
        page=self.project.page(1)
        self.assertTrue(page['image_only_checks'][0]['verdicts'][0]['blocked'])
        for key in ('runs','edits','ai_reviews','ai_decisions','refinement_reviews','refinement_decisions'):
            self.assertEqual(before.get(key),page.get(key))
        self.assertEqual(self.fixture.reopen().page(1),page)

    def test_new_allowed_acceptance_keeps_captured_proof_through_undo(self):
        item=self.build(':','：');checks.record_checks(self.project,1)
        proof=refinement.proposals(self.project,1)[0]['image_only_check']
        self.assertFalse(proof['blocked'])
        refinement.accept_proposal(self.project,1,item['review_id'],item['id'])
        page=self.project.page(1)
        self.assertEqual(page['refinement_decisions'][-1]['image_only_check'],proof)
        self.assertEqual(page['edits'][-1]['image_only_check_sha256'],core._digest(proof))
        self.assertEqual(self.fixture.reopen().page(1),page)
        self.project.undo_edit(1)
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_no_PDF_reference_or_network_reads_in_image_check(self):
        self.build()
        original=builtins.open
        def restricted(path,*args,**kwargs):
            if str(path).lower().endswith(('.pdf','.hwpx','.docx')):
                raise AssertionError('Source text access is forbidden in image-only mode')
            return original(path,*args,**kwargs)
        with patch('pymupdf.open',side_effect=AssertionError('PDF opened')), \
                patch('builtins.open',side_effect=restricted), \
                patch('socket.create_connection',side_effect=AssertionError('Network called')), \
                patch('yeone_ocr.comparison_refinement_source.record_pdf_check',side_effect=AssertionError('Native check called')):
            record=checks.record_checks(self.project,1)
        self.assertFalse(record['native_pdf_text_used'])
        self.assertEqual(record['new_AI_or_OCR_calls'],0)

    def test_AI_request_replays_its_captured_check_after_later_check(self):
        self.build(':','：');first=checks.record_checks(self.project,1)['id']
        plan=prepare_plan(self.project,[1],ReviewPolicy(visual_scope='page',punctuation=True),
                          settings=AISettings('openai','synthetic-only',max_calls=20))
        self.assertTrue(all(batch['refinement_evidence']['proposals'][0]['image_only_check']['image_check_id']==first
                            for batch in plan.batches))
        client,calls=self.client()
        outcome=run_plan(self.project,plan,api_key='public-synthetic',client_factory=lambda *_:client)
        self.assertEqual(outcome['failed'],0);self.assertEqual(len(calls),2)
        reviews=copy.deepcopy(self.project.page(1)['ai_reviews'])
        self.fixture.add('tesseract',self.fixture.text)
        refinement.build_candidates(self.project,1)
        checks.record_checks(self.project,1)
        reopened=self.fixture.reopen()
        self.assertEqual(reopened.page(1)['ai_reviews'],reviews)
        for review in reviews:refinement.validate_ai_refinement_context(reopened.page(1),review['request'])

    def test_new_AI_acceptance_proof_preserves_raw_response_and_undo(self):
        self.build(':','：')
        plan=prepare_plan(self.project,[1],ReviewPolicy(visual_scope='page'),
                          settings=AISettings('openai','synthetic-only',max_calls=20))
        client,_=self.client(lambda data,_:self.response(data,[(4,5,':','：','recognition')]))
        outcome=run_plan(self.project,plan,api_key='public-synthetic',client_factory=lambda *_:client)
        self.assertEqual(outcome['failed'],0)
        raw_reviews=copy.deepcopy(self.project.page(1)['ai_reviews'])
        record=checks.record_checks(self.project,1)
        self.assertTrue(any(v['kind']=='ai' for v in record['verdicts']))
        item=next(p for p in self.project.ai_proposals(1) if p['applicable'])
        proof=item['image_only_check'];self.assertFalse(proof['blocked'])
        self.project.accept_ai_proposal(1,item['review_id'],item['id'])
        page=self.project.page(1)
        self.assertEqual(page['ai_reviews'],raw_reviews)
        self.assertEqual(page['ai_decisions'][-1]['image_only_check'],proof)
        self.assertEqual(page['edits'][-1]['image_only_check_sha256'],core._digest(proof))
        self.assertEqual(self.fixture.reopen().page(1),page)
        self.project.undo_edit(1)
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_explicit_traditional_editorial_request_preserves_its_option_proof(self):
        before='甲乙丙丁汉己庚辛壬'
        self.fixture.text=before
        for engine in ('glmocr','rapidocr','surya'):
            self.fixture.add(engine,before)
        self.project.choose_baseline(1,'glmocr')
        plan=prepare_plan(self.project,[1],ReviewPolicy(visual_scope='page',traditional=True),
                          settings=AISettings('openai','synthetic-only',max_calls=20))
        def operation(data,_):
            patches=[(4,5,'汉','漢','traditional')] if data['phase']=='editorial' else []
            return self.response(data,patches)
        client,calls=self.client(operation)
        outcome=run_plan(self.project,plan,api_key='public-synthetic',client_factory=lambda *_:client)
        self.assertEqual(outcome['failed'],0);self.assertEqual(len(calls),2)
        old_reviews=copy.deepcopy(self.project.page(1)['ai_reviews'])
        record=checks.record_checks(self.project,1)
        self.assertEqual(len(record['verdicts']),1)
        verdict=record['verdicts'][0]
        self.assertFalse(verdict['blocked'])
        self.assertTrue(verdict['source_evidence']['explicit_traditional_editorial'])
        item=next(p for p in self.project.ai_proposals(1) if p['kind']=='traditional')
        self.assertTrue(item['applicable'])
        self.project.accept_ai_proposal(1,item['review_id'],item['id'])
        self.assertEqual(self.project.text(1),before.replace('汉','漢'))
        self.assertEqual(self.project.page(1)['ai_reviews'],old_reviews)
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_draft_uses_saved_image_proposals_without_native_PDF_calls(self):
        item=self.build();before=self.project.page(1)
        with patch('pymupdf.open',side_effect=AssertionError('PDF opened')), \
                patch('yeone_ocr.comparison_refinement_source.record_pdf_check',side_effect=AssertionError('Native check called')):
            draft=checks.evaluate_draft(self.project,1,existing_selected=[item])
        self.assertNotIn(item['id'],{p['id'] for p in draft['selected']})
        self.assertEqual(draft['text'],self.fixture.text)
        self.assertFalse(draft['native_pdf_text_used'])
        for key in ('runs','edits','ai_reviews','ai_decisions','refinement_decisions'):
            self.assertEqual(before.get(key),self.project.page(1).get(key))
        self.assertEqual(self.fixture.reopen().page(1),self.project.page(1))

    def test_draft_cannot_relabel_saved_engine_or_source_provenance(self):
        item=self.build(':','：')
        for key,value in (('engine','surya'),('source_raw_sha256','0'*64),('baseline_run_id','forged')):
            altered={**copy.deepcopy(item),key:value}
            with self.subTest(field=key), self.assertRaises(core.ComparisonError):
                checks.evaluate_draft(self.project,1,existing_selected=[altered])


class ImageCheckUIIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):hosted.HostedComparisonEditor.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):hosted.HostedComparisonEditor.tearDownClass.__func__(cls)

    def setUp(self):
        hosted.HostedComparisonEditor.setUp(self)
        self.fixture=ImageCheckIndependentTests(methodName='runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.item=self.fixture.build();checks.record_checks(self.fixture.project,1)
        self.project=self.editor.project=self.fixture.project
        self.editor._populate_page()

    def test_image_hold_badge_disabled_accept_and_details(self):
        import tkinter as tk
        panel=self.editor.refinement_panel
        key=panel.tree.get_children()[0];panel.tree.selection_set(key);panel.selected()
        self.assertEqual(panel.tree.item(key,'values')[-1],'이미지 근거 보류')
        self.assertTrue(panel.accept_button.instate(['disabled']))
        self.assertIn('이미지 보류 1곳',panel.count.get())
        before=self.project.to_dict();panel.decide(True);self.assertEqual(self.project.to_dict(),before)
        existing=set(self.editor.winfo_children());panel.details()
        window=next(w for w in self.editor.winfo_children() if w not in existing and isinstance(w,tk.Toplevel))
        self.addCleanup(window.destroy)
        text=next(w for w in window.winfo_children() if isinstance(w,tk.Text))
        details=json.loads(text.get('1.0','end'))
        self.assertEqual(details['image_only_checks'],self.project.page(1)['image_only_checks'])
        self.assertTrue(details['selected_proposal']['image_only_check']['blocked'])
        self.assertEqual(str(text.cget('state')),'disabled')

    def test_old_pending_UI_is_readonly_and_held_before_explicit_recheck(self):
        fixture=ImageCheckIndependentTests(methodName='runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.build();self.editor.project=fixture.project
        before=fixture.project.to_dict();self.editor._populate_page()
        panel=self.editor.refinement_panel;key=panel.tree.get_children()[0]
        panel.tree.selection_set(key);panel.selected()
        self.assertEqual(panel.tree.item(key,'values')[-1],'이미지 근거 보류')
        self.assertTrue(panel.accept_button.instate(['disabled']))
        self.assertEqual(fixture.project.to_dict(),before)


if __name__=='__main__':unittest.main()
