import copy
from unittest.mock import patch

import pytest

from tests import test_comparison_refinement as fixtures
from yeone_ocr import comparison as core
from yeone_ocr import comparison_image_only as checks
from yeone_ocr import comparison_refinement as refinement
from yeone_ocr.comparison_incremental_observations import (
    frozen_raw,
    observe_han,
    observe_punctuation,
)


@pytest.fixture
def fixture():
    value = fixtures.RefinementTests(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()


def build(fixture):
    text = "天地玄黃,宇宙洪荒"
    fixture.text = text
    for engine in ("glmocr", "easyocr", "surya", "paddle_server"):
        fixture.add(engine, text if engine == "glmocr" else text.replace(",", "，"))
    fixture.project.choose_baseline(1, "glmocr")
    proposals = refinement.build_candidates(fixture.project, 1,
        policy=refinement.RefinementPolicy(version="anchored-refinement-v3"), use_pdf_text=False)
    item = next(p for p in proposals if p["kind"] == "punctuation")
    checks.record_checks(fixture.project, 1)
    return item


def evaluate(fixture, item):
    return checks.evaluate_incremental_candidate(fixture.project, 1, "refinement", item["review_id"], item["id"])


def test_public_api_preserves_v1_raw_proposals_human_history_revision_and_reopen(fixture):
    item = build(fixture)
    original = fixture.project.page(1)
    revision = fixture.project._revision
    result = evaluate(fixture, item)
    assert result["verdict"]["action"] == "hold_review_only"
    assert result["diagnostic_width_verdict"]["action"] == "retain_unconfirmed"
    assert not result["new_approval"] and not result["human_accepted"]
    result["observation"]["observations"][0]["literal"] = "MUTATED RETURN"
    result["existing_guards"]["passed"] = False
    assert fixture.project.page(1) == original and fixture.project._revision == revision
    assert fixture.reopen().page(1) == original


def test_explicit_diagnostic_mode_replays_frozen_consensus_without_approval(fixture):
    item = build(fixture)
    original, revision = fixture.project.page(1), fixture.project._revision
    safe = evaluate(fixture, item)
    diagnostic = checks.evaluate_incremental_candidate(fixture.project, 1, "refinement",
        item["review_id"], item["id"], diagnostic_width_consensus=True)
    assert safe["verdict"]["action"] == "hold_review_only"
    assert diagnostic["verdict"] == safe["diagnostic_width_verdict"]
    assert diagnostic["observation"] == safe["observation"]
    assert diagnostic["existing_guards"] == safe["existing_guards"]
    assert not diagnostic["new_approval"] and not diagnostic["human_accepted"]
    assert fixture.project.page(1) == original and fixture.project._revision == revision
    with pytest.raises(TypeError):
        checks.evaluate_incremental_candidate(fixture.project, 1, "refinement",
            item["review_id"], item["id"], diagnostic_width_consensus=1)


def test_empty_or_invalid_candidate_failure_does_not_create_page(fixture):
    original, revision = copy.deepcopy(fixture.project._pages), fixture.project._revision
    with pytest.raises(core.ComparisonError):
        checks.evaluate_incremental_candidate(fixture.project, 1, "ai", "missing", "missing")
    assert fixture.project._pages == original and fixture.project._revision == revision


@pytest.mark.parametrize("change", ["accepted", "rejected", "human_edit", "baseline", "parent_run", "source_file"])
def test_already_decided_changed_source_or_stale_candidate_is_not_recalled(fixture, change):
    item = build(fixture)
    if change == "accepted":
        refinement.accept_proposal(fixture.project, 1, item["review_id"], item["id"])
    elif change == "rejected":
        refinement.reject_proposal(fixture.project, 1, item["review_id"], item["id"])
    elif change == "human_edit":
        fixture.project._set_text(1, fixture.text + "人", "synthetic-user-edit")
    elif change == "baseline":
        fixture.project.choose_baseline(1, "easyocr")
    elif change == "parent_run":
        fixture.add("surya", fixture.text)
    else:
        fixture.source.write_bytes(b"source changed")
    original, revision = copy.deepcopy(fixture.project._pages), fixture.project._revision
    with pytest.raises(core.ComparisonError):
        evaluate(fixture, item)
    assert fixture.project._pages == original and fixture.project._revision == revision


def test_existing_image_veto_is_not_overridden_by_exact_width_majority(fixture):
    item = build(fixture)
    original = fixture.project.page(1)
    old_verdicts = checks._verdicts
    def veto(*args):
        records = old_verdicts(*args)
        records[0]["blocked"] = True
        records[0]["reason_codes"] = ["synthetic-existing-veto"]
        return records
    with patch.object(checks, "_verdicts", side_effect=veto):
        result = evaluate(fixture, item)
    assert "existing_guard_not_passed" in result["verdict"]["reason_codes"]
    assert fixture.project.page(1) == original


def test_public_api_never_reads_pdf_or_calls_network(fixture):
    item = build(fixture)
    with patch("pymupdf.open", side_effect=AssertionError("PDF read")), \
            patch("socket.create_connection", side_effect=AssertionError("network")):
        assert evaluate(fixture, item)["new_AI_or_OCR_calls"] == 0


@pytest.mark.parametrize("baseline_engine,other", [("paddle_server", "rapidocr"), ("rapidocr", "paddle_server")])
def test_han_observations_include_other_baseline_family_runs(fixture, baseline_engine, other):
    text = "天地玄黃甲宇宙洪荒"
    for engine, middle in ((baseline_engine, "甲"), (other, "乙"), ("surya", "乙")):
        fixture.add(engine, text.replace("甲", middle))
    fixture.project.choose_baseline(1, baseline_engine)
    page = fixture.project.page(1)
    batch, evidence, _, _ = frozen_raw(page, text)
    proof = observe_han(batch, {"start": 4, "end": 5, "before": "甲", "after": "乙"}, evidence)
    assert proof["family_consistency"]["blocked"]
    assert "paddle" in proof["family_consistency"]["conflicts"]


def test_raw_observation_repeated_anchors_and_target_whitespace_are_not_normalized(fixture):
    text = "天地玄黃, 宇宙洪荒"
    fixture.add("glmocr", text)
    fixture.add("easyocr", "天地玄黃， 宇宙洪荒")
    fixture.add("surya", "天地玄黃，宇宙洪荒天地玄黃，宇宙洪荒")
    fixture.project.choose_baseline(1, "glmocr")
    batch, evidence, _, _ = frozen_raw(fixture.project.page(1), text)
    result = observe_punctuation(batch, {"start": 4, "end": 6, "before": ", ", "after": "，"}, evidence)
    rows = {r["engine"]: r for r in result["observations"]}
    assert rows["surya"]["status"] == "ambiguous"
    assert rows["easyocr"]["literal"] == "， "
    assert rows["easyocr"]["inherited_stance"] == "neither"
