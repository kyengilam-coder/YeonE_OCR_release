"""Keep edits to a saved reading when its machine comparison is rebuilt.

The caller reconstructs the previous result with its recorded text policy and
reading mode, including historical adjudications, before calling this module.
Differences from that baseline are preserved conservatively; this module never
normalizes text or modifies either source record.
"""
from __future__ import annotations

from copy import deepcopy

from .schema import PageReading, Uncertain


# Empty region IDs are invalid, so this can unambiguously identify page fields.
PAGE_SCOPE = ""
STRUCTURE_PRESERVATION_NOTE = (
    "기존 판독 보존 — 저장된 교정문의 영역·문단·열 구조를 새 비교 결과와 "
    "안전하게 대응할 수 없어 이전 교정문 전체를 유지함. 새 비교의 확인 항목은 함께 남김."
)
_PAGE_FIELDS = ("print_page", "running_header", "page_label", "page_note")


def _retain_previous_notice(notice: Uncertain) -> bool:
    # This prefix is reserved for the app's generated pass-2 failure status.
    # Its current status comes only from newly_merged: replaying an old notice
    # as a manual memo would resurrect a failure after a successful retry.
    return not notice.context.startswith("2차 판독 실패(")


def _structure(reading: PageReading) -> tuple:
    """Only the structure needed to match regions and their text containers."""
    return (
        reading.pdf_page,
        tuple(
            (
                region.id, region.kind, region.reading_direction, region.title_level,
                tuple((paragraph.continues_from_previous,
                       tuple(column.col_from_right for column in paragraph.columns))
                      for paragraph in region.paragraphs),
                tuple(len(row.cells) for row in region.table_rows),
            )
            for region in reading.regions
        ),
    )


def _append_notices(target: PageReading, notices: list[Uncertain]) -> None:
    for notice in notices:
        if notice not in target.uncertain:
            target.uncertain.append(notice.model_copy(deep=True))


def preserve_manual_regions(
    previous_record: dict,
    reconstructed_previous: PageReading,
    newly_merged: PageReading,
) -> tuple[PageReading, list[str]]:
    """Return a new reading retaining edits absent from the old reconstruction.

    The returned IDs identify preserved regions; ``PAGE_SCOPE`` (the empty
    string) identifies preserved page fields or a whole-page fallback. Notes
    alone never cause text to be replaced. Newly generated warnings are kept,
    together with previous warnings for preserved regions and notices absent
    from the reconstructed baseline. Reserved generated failure notices follow
    the newly merged result, so successful retries can clear them. All inputs
    remain unchanged.

    If the previous text/structure has no edits, the new result is retained
    even when its structure has changed. If edits cannot safely be matched by
    unique region IDs and container structure, preserve the previous reading
    in full and append an explicit notice.
    """
    result = newly_merged.model_copy(deep=True)
    saved = previous_record.get("merged")
    if saved is None:
        return result, []
    previous = PageReading.model_validate(deepcopy(saved))
    previous_regions = [region.model_dump() for region in previous.regions]
    baseline_regions = [region.model_dump() for region in reconstructed_previous.regions]
    regions_changed = previous_regions != baseline_regions
    page_fields = [name for name in _PAGE_FIELDS
                   if getattr(previous, name) != getattr(reconstructed_previous, name)]
    extra_notices = [notice for notice in previous.uncertain
                     if _retain_previous_notice(notice) and notice not in reconstructed_previous.uncertain]
    if not regions_changed and not page_fields:
        _append_notices(result, extra_notices)
        return result, []

    previous_ids = [region.id for region in previous.regions]
    baseline_ids = [region.id for region in reconstructed_previous.regions]
    result_ids = [region.id for region in result.regions]
    matchable = (
        len(set(previous_ids)) == len(previous_ids)
        and previous_ids == baseline_ids == result_ids
        and _structure(previous) == _structure(reconstructed_previous) == _structure(result)
    )
    if not matchable:
        kept = previous.model_copy(deep=True)
        kept.uncertain = [notice for notice in kept.uncertain if _retain_previous_notice(notice)]
        _append_notices(kept, result.uncertain)
        _append_notices(kept, [Uncertain(context=STRUCTURE_PRESERVATION_NOTE)])
        return kept, [PAGE_SCOPE, *dict.fromkeys(previous_ids)]

    preserved_ids = []
    for index, (saved_region, baseline_region) in enumerate(
        zip(previous.regions, reconstructed_previous.regions)
    ):
        if saved_region != baseline_region:
            result.regions[index] = saved_region.model_copy(deep=True)
            preserved_ids.append(saved_region.id)
    for name in page_fields:
        setattr(result, name, getattr(previous, name))
    _append_notices(result, [notice for notice in previous.uncertain
                             if notice.region_id in preserved_ids and _retain_previous_notice(notice)])
    _append_notices(result, extra_notices)
    if page_fields:
        preserved_ids.append(PAGE_SCOPE)
    return result, preserved_ids
