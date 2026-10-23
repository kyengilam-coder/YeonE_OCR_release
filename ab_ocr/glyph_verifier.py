"""Conservative local printed-glyph recognition, separate from EasyOCR weights.

Uses installed fonts in place; no font, reference document or template bank is
downloaded or distributed. The bank covers the full BMP CJK unified/Extension A
ranges, not a list of corrections. Absence of these fonts disables this stage.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
import os
from pathlib import Path

POLICY = "installed-cjk-fonts-native-correlation-v1"
MIN_SIMILARITY = 0.98
MIN_MARGIN = 0.03
FONT_NAMES = ("simsun.ttc", "msjh.ttc")
CJK_RANGES = ((0x3400, 0x4dc0), (0x4e00, 0xa000))


def font_sources():
    """Pure stdlib dependency fingerprints, also used in host cache requests."""
    if os.name != "nt":
        return []
    directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    sources = []
    for name in FONT_NAMES:
        path = directory / name
        if path.is_file() and path.stat().st_size <= 128_000_000:
            sources.append({"path": str(path.resolve()), "face_index": 0,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return sources


def _trim(ink):
    import numpy as np
    rows, cols = np.nonzero(ink > 32)
    if not len(rows):
        return None
    return ink[rows.min():rows.max()+1, cols.min():cols.max()+1]


def _vector(ink):
    import cv2
    import numpy as np
    ink = _trim(ink)
    if ink is None:
        return None
    h, w = ink.shape
    scale = 30 / max(h, w)
    small = cv2.resize(ink, (max(1, round(w*scale)), max(1, round(h*scale))),
                       interpolation=cv2.INTER_AREA)
    h, w = small.shape
    square = np.zeros((32, 32), np.float32)
    square[(32-h)//2:(32-h)//2+h, (32-w)//2:(32-w)//2+w] = small / 255
    vector = square.ravel()
    return vector / np.linalg.norm(vector)


class GlyphVerifier:
    def __init__(self, sources):
        self.sources = [dict(source) for source in sources]
        self.matrix = None
        self.labels = []
        self._fonts = OrderedDict()
        self._horizontal_tiles = OrderedDict()

    def _font(self, index, size):
        from PIL import ImageFont
        key = (index, size)
        if key in self._fonts:
            self._fonts.move_to_end(key)
            return self._fonts[key]
        source = self.sources[index]
        font = ImageFont.truetype(source["path"], size, index=source["face_index"])
        self._fonts[key] = font
        if len(self._fonts) > 32:
            self._fonts.popitem(last=False)
        return font

    def _mask(self, index, char, size):
        import numpy as np
        mask = self._font(index, size).getmask(char, mode="L")
        if not all(mask.size):
            return None
        return _trim(np.asarray(mask, dtype=np.uint8).reshape(mask.size[1], mask.size[0]))

    def _build(self):
        import numpy as np
        # Preallocate to bound peak memory; two fonts need at most ~226 MB.
        capacity = len(self.sources) * sum(end-start for start, end in CJK_RANGES)
        matrix = np.empty((capacity, 1024), np.float32)
        count = 0
        for index, source in enumerate(self.sources):
            if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("Glyph font changed before recognition")
            font = self._font(index, 64)
            missing = font.getmask(chr(0x10ffff), mode="L")
            missing = (missing.size, bytes(missing))
            for start, end in CJK_RANGES:
                for code in range(start, end):
                    char = chr(code)
                    mask = font.getmask(char, mode="L")
                    if not all(mask.size) or (mask.size, bytes(mask)) == missing:
                        continue
                    ink = np.asarray(mask, dtype=np.uint8).reshape(mask.size[1], mask.size[0])
                    vector = _vector(ink)
                    if vector is not None:
                        matrix[count] = vector
                        self.labels.append(char)
                        count += 1
            if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("Glyph font changed during recognition")
        self.matrix = matrix[:count]

    def _native_score(self, target, char, index):
        import cv2
        import numpy as np
        prototype = self._mask(index, char, 64)
        if prototype is None:
            return 0.0
        h, w = target.shape
        estimate = 64 * h / prototype.shape[0]
        best = 0.0
        canvas = np.pad(target.astype(np.float32)/255, 3)
        # Fit only font size and a small translation. Do not stretch strokes to
        # make a different character look like the source.
        for size in range(max(8, math.floor(estimate)-2), min(512, math.ceil(estimate)+2)+1):
            ink = self._mask(index, char, size)
            if ink is None or abs(ink.shape[0]-h) > 2 or abs(ink.shape[1]-w) > 2:
                continue
            scores = cv2.matchTemplate(canvas, ink.astype(np.float32)/255, cv2.TM_CCORR_NORMED)
            best = max(best, min(1.0, float(cv2.minMaxLoc(scores)[1])))
        return best

    def match(self, crop, incumbent):
        import numpy as np
        record = {"accepted": False, "incumbent": incumbent,
                  "source_pixels_sha256": hashlib.sha256(crop.tobytes()).hexdigest()}
        if not self.sources:
            return {**record, "status": "no installed font sources"}
        ink = _trim(255-crop)
        if ink is None or max(ink.shape) > 512 or np.mean(crop < 128) > 0.8:
            return {**record, "status": "blank, oversized or non-light-background crop"}
        if self.matrix is None:
            self._build()
        if not len(self.labels):
            return {**record, "status": "no supported glyphs"}
        scores = self.matrix @ _vector(ink)
        count = min(16, len(scores))
        top = np.argpartition(scores, -count)[-count:]
        candidates = {self.labels[int(i)] for i in top}
        candidates.add(incumbent)
        ranked = []
        for char in sorted(candidates):
            matches = [self._native_score(ink, char, i) for i in range(len(self.sources))]
            font_index = max(range(len(matches)), key=matches.__getitem__)
            ranked.append({"text": char, "similarity": matches[font_index], "font_index": font_index})
        ranked.sort(key=lambda item: (-item["similarity"], item["text"]))
        winner = ranked[0]
        runner = ranked[1]["similarity"] if len(ranked) > 1 else 0.0
        margin = winner["similarity"] - runner
        accepted = (winner["text"] != incumbent and winner["similarity"] >= MIN_SIMILARITY
                    and margin >= MIN_MARGIN)
        return {**record, "status": "checked", "accepted": accepted, "candidate": winner["text"],
                "similarity": winner["similarity"], "margin": margin,
                "font_index": winner["font_index"], "ranked": ranked,
                "bank_entries": len(self.labels)}


def refine_region(verifier, gray, segments, text):
    evidence = {"input_text": text, "changes": [], "glyphs": []}
    if len(segments) != len(text):
        return text, {**evidence, "status": "skipped: text and segment counts differ"}
    output = list(text)
    for index, (char, segment) in enumerate(zip(text, segments)):
        if not (0x3400 <= ord(char) <= 0x4dbf or 0x4e00 <= ord(char) <= 0x9fff):
            continue
        left, right, top, bottom = segment["box"]
        if not 0.5 < (right-left)/(bottom-top) < 1.8:
            continue
        decision = verifier.match(gray[top:bottom, left:right], char)
        decision.update({"index": index, "source_bbox": [left, top, right, bottom]})
        evidence["glyphs"].append(decision)
        if decision["accepted"]:
            output[index] = decision["candidate"]
            evidence["changes"].append({"index": index, "before": char, "after": output[index]})
    return "".join(output), {**evidence, "status": "checked"}


def horizontal_groups(words):
    """Join consecutive, close fragments of one row; do not cross columns."""
    groups = []
    for index, word in enumerate(words):
        left, top, right, bottom = word["bbox"]
        if groups:
            previous = groups[-1]["source_bbox"]
            h, old_h = bottom-top, previous[3]-previous[1]
            overlap = min(bottom, previous[3])-max(top, previous[1])
            gap = left-previous[2]
            if (overlap > 0.5*min(h, old_h) and max(h, old_h) < 1.8*min(h, old_h)
                    and 0 <= gap < min(h, old_h)):
                groups[-1]["indices"].append(index)
                groups[-1]["source_bbox"] = [min(left, previous[0]), min(top, previous[1]),
                                             max(right, previous[2]), max(bottom, previous[3])]
                continue
        groups.append({"source_bbox": list(word["bbox"]), "indices": [index]})
    return groups


def _horizontal_tile(verifier, font_index, size, char, height, baseline):
    from PIL import Image, ImageDraw
    import numpy as np
    key = (font_index, size, char, height, baseline)
    cache = verifier._horizontal_tiles
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    font = verifier._font(font_index, size)
    advance = float(font.getlength(char))
    image = Image.new("L", (max(1, math.ceil(advance)), height), 255)
    ImageDraw.Draw(image).text((0, baseline), char, font=font, fill=0)
    value = (255-np.asarray(image), advance)
    cache[key] = value
    if len(cache) > 4096:
        cache.popitem(last=False)
    return value


def _horizontal_anchor(verifier, crop, first):
    import cv2
    import numpy as np
    ink = _trim(255-crop)
    if ink is None:
        return None
    height, width = crop.shape
    anchors = []
    search = (255-crop[:, :min(width, round(height*1.6))]).astype(np.float32)
    for index in range(len(verifier.sources)):
        prototype = verifier._mask(index, first, 64)
        if prototype is None:
            continue
        estimate = 64*ink.shape[0]/prototype.shape[0]
        for size in range(max(8, math.floor(estimate)-5), min(512, math.ceil(estimate)+5)+1):
            font = verifier._font(index, size)
            mask = font.getmask(first, mode="L")
            if not all(mask.size):
                continue
            array = np.asarray(mask, dtype=np.uint8).reshape(mask.size[1], mask.size[0])
            rows, cols = np.nonzero(array > 32)
            if not len(rows):
                continue
            template = array[rows.min():rows.max()+1, cols.min():cols.max()+1]
            if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
                continue
            scores = cv2.matchTemplate(search, template.astype(np.float32), cv2.TM_CCORR_NORMED)
            _, score, _, location = cv2.minMaxLoc(scores)
            bounds = font.getbbox(first)
            anchors.append({"similarity": min(1.0, float(score)), "font_index": index, "size": size,
                "x": int(location[0]-bounds[0]-cols.min()),
                "y": int(location[1]-bounds[1]-rows.min())})
    return max(anchors, key=lambda item: item["similarity"]) if anchors else None


def _horizontal_line(verifier, crop, first):
    """Read glyphs with advances and verify the complete rendered line.

    Full-width punctuation is recognized from both shape and placement, never
    obtained by normalizing ASCII text. Real spaces/ambiguous cells abstain.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    height, width = crop.shape
    source_hash = hashlib.sha256(crop.tobytes()).hexdigest()
    evidence = {"accepted": False, "source_pixels_sha256": source_hash, "glyphs": []}
    if not verifier.sources:
        return None, {**evidence, "status": "no installed font sources"}
    if not (0x3400 <= ord(first) <= 0x4dbf or 0x4e00 <= ord(first) <= 0x9fff):
        return None, {**evidence, "status": "no leading Han anchor"}
    if height > 512 or width > 8192 or width*height > 8_000_000 or width < height:
        return None, {**evidence, "status": "unsupported line geometry"}
    if verifier.matrix is None:
        verifier._build()
    if not len(verifier.labels):
        return None, {**evidence, "status": "no supported glyphs"}
    anchor = _horizontal_anchor(verifier, crop, first)
    if anchor is None or anchor["similarity"] < MIN_SIMILARITY:
        return None, {**evidence, "status": "unverified font/size/origin", "anchor": anchor}
    evidence["anchor"] = anchor
    font_index, size = anchor["font_index"], anchor["size"]
    start, baseline = anchor["x"], anchor["y"]
    if start < 0 or np.any(crop[:, :start] < 220):
        return None, {**evidence, "status": "anchor would omit leading ink"}
    punctuation = {chr(code) for a, b in ((33, 127), (0x2010, 0x2028),
                                          (0x3001, 0x3040), (0xff01, 0xff61)) for code in range(a, b)}
    pen, output = float(start), []
    for _ in range(2048):
        pos = round(pen)
        if pos >= width or not np.any(crop[:, pos:] < 220):
            break
        end = min(width, pos+math.ceil(verifier._font(font_index, size).getlength("漢")))
        vector = _vector(255-crop[:, pos:end])
        if vector is None:
            return None, {**evidence, "status": "blank or spaced cell"}
        scores = verifier.matrix @ vector
        count = min(16, len(scores))
        top = np.argpartition(scores, -count)[-count:]
        candidates = {verifier.labels[int(i)] for i in top} | punctuation
        ranked = []
        for char in sorted(candidates):
            ink, advance = _horizontal_tile(verifier, font_index, size, char, height, baseline)
            if advance <= 0:
                continue
            target = np.zeros_like(ink)
            take = min(ink.shape[1], width-pos)
            target[:, :take] = 255-crop[:, pos:pos+take]
            a, b = ink.astype(np.float32).ravel(), target.astype(np.float32).ravel()
            denominator = float(np.linalg.norm(a)*np.linalg.norm(b))
            score = min(1.0, float(a@b)/denominator) if denominator else 0.0
            ranked.append({"text": char, "similarity": score, "advance": advance})
        ranked.sort(key=lambda item: (-item["similarity"], item["text"]))
        if len(ranked) < 2:
            return None, {**evidence, "status": "no distinct candidates"}
        winner = ranked[0]
        margin = winner["similarity"]-ranked[1]["similarity"]
        if winner["similarity"] < MIN_SIMILARITY or margin < MIN_MARGIN:
            return None, {**evidence, "status": "ambiguous cell", "abstained_at": pos,
                          "abstained_candidates": ranked[:5]}
        output.append(winner["text"])
        evidence["glyphs"].append({**winner, "margin": margin, "cell_start": pen,
                                    "cell_end": pen+winner["advance"], "ranked": ranked[:5]})
        pen += winner["advance"]
    if round(pen) < width and np.any(crop[:, round(pen):] < 220):
        return None, {**evidence, "status": "unconsumed ink"}
    text = "".join(output)
    rendered = Image.new("L", (width, height), 255)
    ImageDraw.Draw(rendered).text((start, baseline), text, font=verifier._font(font_index, size), fill=0)
    a = (255-np.asarray(rendered)).astype(np.float32).ravel()
    b = (255-crop).astype(np.float32).ravel()
    denominator = float(np.linalg.norm(a)*np.linalg.norm(b))
    similarity = min(1.0, float(a@b)/denominator) if denominator else 0.0
    evidence.update({"rendered_pixels_sha256": hashlib.sha256(np.asarray(rendered).tobytes()).hexdigest(),
                     "line_similarity": similarity, "candidate_text": text,
                     "bank_entries": len(verifier.labels)})
    if not text or similarity < MIN_SIMILARITY:
        return None, {**evidence, "status": "full-line rendering did not verify"}
    return text, {**evidence, "accepted": True, "status": "verified complete line"}


def refine_horizontal(verifier, gray, words):
    """Keep original regions on abstention; retain all neural source evidence."""
    result, evidence = [], []
    for group in horizontal_groups(words):
        left, top, right, bottom = group["source_bbox"]
        left, top, right, bottom = math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)
        original = [words[i] for i in group["indices"]]
        first_text = original[0]["text"]
        record = {**group, "source_bbox": [left, top, right, bottom],
                  "input_text": "\n".join(word["text"] for word in original)}
        if not first_text:
            text, proof = None, {"accepted": False, "status": "empty source reading"}
        else:
            text, proof = _horizontal_line(verifier, gray[top:bottom, left:right], first_text[0])
        evidence.append({**record, "glyph_verification": proof})
        if text is None:
            result.extend(original)
        else:
            result.append({"text": text, "bbox": [left, top, right, bottom], "confidence": None,
                           "block": 1, "paragraph": 1, "line": len(result)+1})
    return result, evidence


def _vertical_segments(gray, box):
    """Whitespace segmentation of a column envelope, without duplicate overlaps."""
    import cv2
    import numpy as np
    left, top, right, bottom = box
    width, height = right-left, bottom-top
    if height < 1.8*width:
        return [{"box": [left, right, top, bottom]}]
    crop = gray[top:bottom, left:right]
    _, mask = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    rows = np.flatnonzero(np.any(mask, axis=1))
    groups = []
    for row in rows:
        if groups and row-groups[-1][1] <= max(2, round(width*.25)):
            groups[-1][1] = int(row)+1
        else:
            groups.append([int(row), int(row)+1])
    if len(groups) < 2:
        return [{"box": [left, right, top, bottom]}]
    cuts = [0]+[(a[1]+b[0])//2 for a, b in zip(groups, groups[1:])]+[height]
    segments = []
    for index, (start, end) in enumerate(groups):
        center = (start+end)/2
        half = max(width/2, (end-start)/2+max(2, round(width*.1)))
        a = top+max(cuts[index], math.floor(center-half))
        b = top+min(cuts[index+1], math.ceil(center+half))
        segments.append({"box": [left, right, a, b]})
    return segments


def refine_vertical_columns(verifier, gray, words, columns):
    """Coalesce only fully verified nearby column fragments; abstain as a group.

    Segment the union of overlapping detections once so a fragment's partial
    neighboring glyph cannot become an extra character. Every glyph, including
    unchanged ones, needs strong visual evidence before line breaks are merged.
    """
    result, evidence = [], []
    for column in columns:
        chunks = []
        for index in column:
            box = words[index]["bbox"]
            if chunks:
                previous = words[chunks[-1][-1]]["bbox"]
                if box[1]-previous[3] <= 1.8*max(box[2]-box[0], previous[2]-previous[0]):
                    chunks[-1].append(index)
                    continue
            chunks.append([index])
        for indices in chunks:
            original = [words[i] for i in indices]
            box = [math.floor(min(w["bbox"][0] for w in original)),
                   math.floor(min(w["bbox"][1] for w in original)),
                   math.ceil(max(w["bbox"][2] for w in original)),
                   math.ceil(max(w["bbox"][3] for w in original))]
            text = "".join(w["text"] for w in original)
            record = {"indices": indices, "source_bbox": box,
                      "input_text": "\n".join(w["text"] for w in original)}
            if (box[2]-box[0] > 512 or (box[2]-box[0])*(box[3]-box[1]) > 8_000_000):
                result.extend(original)
                evidence.append({**record, "accepted": False, "status": "unsupported column geometry"})
                continue
            segments = _vertical_segments(gray, box)
            refined, proof = refine_region(verifier, gray, segments, text)
            verified = (bool(text) and len(segments) == len(text) == len(proof["glyphs"])
                and all(g.get("status") == "checked" and g.get("similarity", 0) >= MIN_SIMILARITY
                        and g.get("margin", 0) >= MIN_MARGIN
                        and (g["candidate"] == g["incumbent"] or g["accepted"]) for g in proof["glyphs"]))
            evidence.append({**record, "segments": [{"source_bbox": [s["box"][0], s["box"][2], s["box"][1], s["box"][3]]}
                                                    for s in segments],
                             "glyph_refinement": proof, "accepted": verified,
                             "status": "all column glyphs verified" if verified else "column retained: incomplete visual evidence"})
            if verified:
                result.append({"text": refined, "bbox": box, "confidence": None,
                               "block": 1, "paragraph": 1, "line": len(result)+1})
            else:
                result.extend(original)
    return result, evidence
