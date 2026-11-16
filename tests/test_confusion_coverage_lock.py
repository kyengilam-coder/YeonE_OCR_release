import copy
import json
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest
from yeone_ocr import confusion_memory as memory
from yeone_ocr.file_lock import exclusive_file_lock
from test_confusion_memory import Fixture, RoutingTests, ledger


def _writer(path, barrier, name):
    barrier.wait(timeout=20)
    def mutate(data):
        time.sleep(.15)  # both writers would otherwise read the same old snapshot
        data['documents'][name] = {'at':'2026-11-15','source':name,'pages':1,'engines':{}}
    memory._update(mutate,path)


def _hold(lock, ready):
    with exclusive_file_lock(lock):
        ready.set()
        time.sleep(1)
        os._exit(0)  # OS must release even without finally


def test_process_updates_preserve_both_and_crash_releases_lock(tmp_path):
    ctx=mp.get_context('spawn'); barrier=ctx.Barrier(2); path=tmp_path/'ledger.json'
    children=[ctx.Process(target=_writer,args=(path,barrier,name)) for name in ['one','two']]
    for p in children:p.start()
    for p in children:
        p.join(30)
        assert p.exitcode==0
    assert set(memory.read_store(path)[0]['documents'])=={'one','two'}
    ready=ctx.Event(); lock=path.with_name(path.name+'.lock')
    child=ctx.Process(target=_hold,args=(lock,ready)); child.start()
    assert ready.wait(15)
    with pytest.raises(TimeoutError):
        with exclusive_file_lock(lock,timeout=.1): pass
    child.join(15); assert child.exitcode==0
    memory.set_enabled(False,path)
    data,_=memory.read_store(path)
    assert not data['enabled'] and set(data['documents'])=={'one','two'}


class CoverageTests(Fixture):
    def page_with_review(self, text='甲己丙己', uncertain=False):
        self.add(text=text)
        page=self.project.page(1); base=page['runs'][0]
        page['ai_reviews']=[{'status':'completed','request':{'phase':'visual','baseline_run_id':base['id'],
            'source_sha256':self.project.source_sha256,'snapshot_text':text,
            'targets':[{'id':'target','start':3,'end':4,'before':text[3:4]}]},
            'response_text':json.dumps({'items':[{'target_id':'target','uncertain':uncertain}]})}]
        return page

    def test_only_reviewed_offsets_count_and_retries_do_not_add_denominator(self):
        page=self.page_with_review()
        page['ai_reviews']*=3
        self.assertEqual(memory.page_observations(page)['seen'], {'己':1})

    def test_uncertain_editorial_failed_and_stale_reviews_not_coverage(self):
        page=self.page_with_review(uncertain=True)
        self.assertIsNone(memory.page_observations(page))
        for name,value in [('phase','editorial'),('baseline_run_id','stale'),('source_sha256','stale')]:
            candidate=self.page_with_review()
            candidate['ai_reviews'][0]['request'][name]=value
            self.assertIsNone(memory.page_observations(candidate))
        candidate=self.page_with_review(); candidate['ai_reviews'][0]['status']='failed'
        self.assertIsNone(memory.page_observations(candidate))

    def test_explicit_edit_only_counts_changed_span_and_multi_occurrence(self):
        self.add(text='甲口口丙口口')
        self.project.set_text(1,'甲回丙口口')
        observed=memory.page_observations(self.project.page(1))
        self.assertEqual(observed['seen'], {'口':2,'口口':1})

    def test_legacy_preserved_backed_up_and_not_used_as_scoped_rate(self):
        old=ledger(); del old['documents']['d'*32]['engines']['ppocrv6']['coverage_basis']
        self.store.write_text(json.dumps(old),encoding='utf-8'); original=self.store.read_bytes()
        self.assertFalse(memory.LearnedConfusions.load(self.store).rows[0].rate_known)
        self.add(); self.project.set_text(1,'甲已丙')
        memory.harvest_project(self.project,self.store)
        self.assertEqual(self.store.with_name(self.store.name+'.before-verified-spans.json').read_bytes(),original)
        data,_=memory.read_store(self.store)
        self.assertEqual(data['documents']['d'*32], old['documents']['d'*32])


class MultiRoutingTests(RoutingTests):
    def test_multi_character_across_word_boxes_routes_and_supplies_hint(self):
        self.add(text='甲口口丙',words=[{'text':c,'bbox':[20+i*20,20,40+i*20,45],'confidence':99} for i,c in enumerate('甲口口丙')])
        learned=memory.LearnedConfusions.from_store(ledger(read='口口',actual='回',human=3,seen=10))
        before=copy.deepcopy(self.project.to_dict())
        plan=self.plan(learned)
        target=plan.batches[0]['targets'][0]
        self.assertEqual(target['before'],'口口')
        self.assertEqual(target['bbox'],[40,20,80,45])
        self.assertEqual(target['learned_confusions'][0]['verified_alternatives'],['回'])
        self.assertEqual(self.project.to_dict(),before)
