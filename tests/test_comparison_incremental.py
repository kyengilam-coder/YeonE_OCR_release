import copy

import pytest

from yeone_ocr.comparison_incremental import (
    assess_family_consistency,
    assess_width_recall,
)


def reading(engine, literal="，", *, status="unique", complete=True, suffix=""):
    return {"engine": engine, "run_id": engine + suffix, "status": status,
            "generation_complete": complete, "literal": literal}


def check(rows, before=",", after="，", **kwargs):
    return assess_width_recall(before, after, rows, baseline_engine="glmocr",
                              source_address_valid=kwargs.get("address", True),
                              existing_guards_passed=kwargs.get("guards", True))


def test_two_exact_independent_families_outweigh_baseline_without_mutation():
    rows = [reading("paddle_server"), reading("surya"), reading("glmocr", ",")]
    old = copy.deepcopy(rows)
    result = check(rows)
    assert result["action"] == "retain_unconfirmed"
    assert result["baseline_conservative_countervotes"] == 1
    assert not result["human_accepted"] and not result["image_encoding_proved"]
    assert rows == old


def test_rapid_and_paddle_are_one_family_and_same_engine_repeats_add_no_vote():
    assert check([reading("rapidocr"), reading("paddle_server")])["action"] == "hold_review_only"
    assert check([reading("surya"), reading("surya", suffix="2")])["action"] == "hold_review_only"


def test_baseline_breaks_tie_and_three_families_can_outweigh_one_other_before():
    rows = [reading("paddle_server"), reading("surya"), reading("easyocr", ",")]
    assert "no_strict_majority_over_baseline_and_before" in check(rows)["reason_codes"]
    rows.append(reading("tesseract"))
    assert check(rows)["action"] == "retain_unconfirmed"


@pytest.mark.parametrize("bad", [reading("easyocr", "、"), reading("easyocr", status="ambiguous"),
                                 reading("easyocr", complete=False), reading("easyocr", status="incomplete")])
def test_third_ambiguous_or_incomplete_veto_even_with_three_after_families(bad):
    rows = [reading("paddle_server"), reading("surya"), reading("tesseract"), bad]
    assert check(rows)["action"] == "hold_review_only"


def test_missing_is_abstention_and_never_a_positive_vote():
    missing = reading("easyocr", status="missing")
    assert check([reading("paddle_server"), reading("surya"), missing])["action"] == "retain_unconfirmed"
    assert check([reading("paddle_server"), missing])["action"] == "hold_review_only"


def test_family_split_veto_cannot_be_outvoted():
    rows = [reading("paddle_server"), reading("rapidocr", ","), reading("surya"), reading("tesseract")]
    assert "within_family_disagreement" in check(rows)["reason_codes"]


def test_target_whitespace_is_literal_and_whitespace_only_remains_held():
    assert check([reading("paddle_server"), reading("surya")], before=", ")["action"] == "retain_unconfirmed"
    assert check([reading("paddle_server", "， "), reading("surya", "， ")], before=", ")["action"] == "hold_review_only"
    assert "whitespace_only_remains_held" in check([reading("surya", ",")], before=", ", after=",")["reason_codes"]


@pytest.mark.parametrize("kwargs", [{"address": False}, {"guards": False}])
def test_prior_guards_and_unique_source_address_cannot_be_overridden(kwargs):
    assert check([reading("paddle_server"), reading("surya")], **kwargs)["action"] == "hold_review_only"


@pytest.mark.parametrize("before,after", [("鬰", "鬱"), ("金", "金"), ("Ａ", "A"), ("１", "1"), ("。", ".")])
def test_no_han_or_general_nfkc_or_kind_rewrite(before, after):
    assert check([reading("paddle_server", after), reading("surya", after)], before, after)["action"] == "hold_review_only"


def test_duplicate_identity_rejected_and_baseline_family_cannot_support():
    row = reading("surya")
    with pytest.raises(ValueError):
        check([row, row])
    assert check([reading("surya"), reading("glmocr")])["action"] == "hold_review_only"


def test_han_family_consistency_is_hold_only_and_literal_including_third_forms():
    for left, right in (("蒌", "萎"), ("金", "金"), ("濫", "渡"), ("字", " 字")):
        result = assess_family_consistency([reading("rapidocr", left), reading("paddle_server", right)])
        assert result["blocked"] and not result["new_approval"]
    result = assess_family_consistency([reading("rapidocr", "甲"), reading("paddle_server", "甲"),
                                        reading("surya", "乙")])
    assert result["action"] == "no_additional_hold" and not result["new_approval"]
