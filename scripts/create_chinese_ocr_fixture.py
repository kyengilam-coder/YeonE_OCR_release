"""Create original, image-only Chinese OCR samples and exact UTF-8 references.

Requires Pillow and reportlab. No document or font is downloaded. Pass a local
CJK TrueType font with --font on non-Windows systems. The PDF embeds raster
images only, so an engine cannot obtain the answers from a PDF text layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

CASES = [
    ("simplified", "horizontal", [
        "春天的清晨，医生打开书本，认真核对每一个汉字。",
        "这份资料用于比较文字识别结果，不作为医学建议。",
        "药材名称：当归、黄芪、白术、茯苓、甘草、生姜。",
        "学习中文时，应当保留原文，不要自动改写或补字。",
        "记录编号：2026-10-24；数量：125；比例：3.5%。",
        "容易混淆的字：日曰、未末、土士、人入、己已巳。",
        "简繁对照：龙龍、医醫、药藥、书書、体體、发發髮。",
    ]),
    ("traditional", "horizontal", [
        "春天的清晨，醫生打開書本，認真核對每一個漢字。",
        "這份資料用於比較文字辨識結果，不作為醫學建議。",
        "藥材名稱：當歸、黃芪、白朮、茯苓、甘草、生薑。",
        "學習中文時，應當保留原文，不要自動改寫或補字。",
        "異體與近形：臺台、裏裡、羣群、爲為、乾幹干。",
        "容易混淆的字：日曰、未末、土士、人入、己已巳。",
        "標點測試：「原文」『引文』，句號。分號；問號？",
    ]),
    ("traditional", "vertical", [
        "天地之間萬物各有所宜",
        "讀書當辨字形不可臆改",
        "當歸黃芪白朮茯苓甘草",
        "生薑乾薑不可混爲一字",
        "日曰未末土士人入己已巳",
        "保留原文異體以便校勘",
    ]),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("output/pdf/chinese_ocr_fixture"))
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/simsun.ttc"))
    args = parser.parse_args(argv)
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    args.out.mkdir(parents=True, exist_ok=True)
    pdf = args.out / "chinese_ocr_fixture.pdf"
    writer = canvas.Canvas(str(pdf), pagesize=(595.28, 841.89), invariant=1)
    font = ImageFont.truetype(str(args.font), 48)
    manifest = {"schema_version": 1, "origin": "Original synthetic text, not a historical scan",
                "text_policy": "Exact code points; no simplified/traditional/variant conversion",
                "pdf": pdf.name, "image_dpi": 200, "pages": []}
    for number, (profile, direction, lines) in enumerate(CASES, 1):
        image = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(image)
        if direction == "horizontal":
            for i, line in enumerate(lines):
                draw.text((105, 180 + i * 160), line, font=font, fill="black")
        else:
            for column, line in enumerate(lines):
                for row, char in enumerate(line):
                    draw.text((1450 - column * 220, 180 + row * 90), char, font=font, fill="black")
        image_path = args.out / f"page_{number:02d}.png"
        image.save(image_path, dpi=(200, 200))
        reference = "\n".join(lines)
        reference_path = args.out / f"page_{number:02d}.reference.txt"
        reference_path.write_text(reference, encoding="utf-8", newline="\n")
        writer.drawImage(ImageReader(image), 0, 0, width=595.28, height=841.89)
        writer.showPage()
        manifest["pages"].append({"page": number, "profile": profile, "direction": direction,
            "image": image_path.name, "reference": reference_path.name,
            "reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
            "reading_order": "top-to-bottom columns, right-to-left" if direction == "vertical" else "top-to-bottom lines"})
    writer.save()
    manifest["pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(pdf.resolve())


if __name__ == "__main__":
    main()
