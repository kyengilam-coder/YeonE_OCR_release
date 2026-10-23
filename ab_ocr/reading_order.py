"""Geometry-only ordering of validated OCR lines; never changes their text."""
from __future__ import annotations

import copy

VERTICAL_RTL_POLICY = "vertical-columns-rtl-top-to-bottom-v1"
VERTICAL_RTL_ENGINES = frozenset({"rapidocr", "easyocr", "ppocrv6"})


def vertical_column_indices(words):
    """Return column groups right-to-left, with fragments top-to-bottom.

    Boxes are validated xyxy image coordinates. A column's common horizontal
    intersection must overlap at least half of the narrower interval. Keeping
    the intersection (rather than the union) avoids joining adjacent columns
    through a chain of slightly shifted fragments. This handles upright,
    single-region vertical text; mixed layouts and interlinear notes still need
    review. Equal positions retain the engine's original order.
    """
    def center(index):
        box = words[index]["bbox"]
        return (box[0] + box[2]) / 2

    columns = []
    for index in sorted(range(len(words)), key=lambda i: (-center(i), words[i]["bbox"][1], i)):
        left, _top, right, _bottom = words[index]["bbox"]
        if columns:
            column = columns[-1]
            overlap = min(right, column["right"]) - max(left, column["left"])
            minimum = min(right - left, column["right"] - column["left"])
            if overlap >= 0.5 * minimum:
                column["left"] = max(left, column["left"])
                column["right"] = min(right, column["right"])
                column["indices"].append(index)
                continue
        columns.append({"left": left, "right": right, "indices": [index]})
    return [sorted(column["indices"], key=lambda i: (words[i]["bbox"][1], -center(i), i))
            for column in columns]


def vertical_line_indices(words):
    return [index for column in vertical_column_indices(words) for index in column]


def order_vertical_payload(payload):
    """Copy a worker result, retaining the pre-order text and boxes as evidence."""
    if payload["text"] != "\n".join(word["text"] for word in payload["words"]):
        raise ValueError("OCR line text does not match the worker's full text")
    result = copy.deepcopy(payload)
    indices = vertical_line_indices(result["words"])
    metadata = result["engine_metadata"]
    metadata["raw_engine_result"] = {"text": result["text"], "words": result["words"],
                                     "reading_order": metadata.get("reading_order")}
    if "text_join_policy" in metadata:
        metadata["raw_engine_result"]["text_join_policy"] = metadata["text_join_policy"]
        metadata["text_join_policy"] = "geometry order; newline between detected boxes"
    result["words"] = [dict(copy.deepcopy(result["words"][index]), line=line)
                       for line, index in enumerate(indices, 1)]
    result["text"] = "\n".join(word["text"] for word in result["words"])
    metadata["reading_order_policy"] = VERTICAL_RTL_POLICY
    metadata["reading_order"] = "right-to-left columns; top-to-bottom fragments within each column"
    metadata["source_line_indices"] = indices  # zero-based positions in raw_engine_result.words
    metadata["text_policy"] = "unchanged line contents ordered by geometry and joined with LF; no character conversion"
    return result
