"""Check the Gemini plan through Antigravity CLI (agy) the way 연어OCR calls it: one request with an image
and a JSON Schema, through yeone_ocr.antigravity_subscription. Run on the PC where agy is signed in:

    python tools/check_antigravity.py                 # draws 天地玄黃 宇宙洪荒 and asks agy to read it
    python tools/check_antigravity.py page.png        # or reads an image of your own
    python tools/check_antigravity.py --model gemini-3.8-flash-high

Prints the program found, the answer, the model and tokens, and for the drawn image whether every
character came back. Sends only the image and this prompt; reads no settings, keys or documents.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED = "天地玄黃宇宙洪荒"
FONTS = ("C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/mingliub.ttc", "C:/Windows/Fonts/batang.ttc",
         "C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}, "seen_image": {"type": "boolean"}},
          "required": ["text", "seen_image"], "additionalProperties": False}


def drawn_image() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    font = next((ImageFont.truetype(path, 72) for path in FONTS if Path(path).is_file()), None)
    if font is None:
        raise SystemExit("한자 글꼴을 찾지 못했습니다. 그림 파일 경로를 직접 주세요: python tools/check_antigravity.py page.png")
    image = Image.new("RGB", (720, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 30), EXPECTED[:4], font=font, fill="black")
    draw.text((40, 130), EXPECTED[4:], font=font, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", nargs="?", type=Path, help="PNG image to read (default: a drawn line of Hanja)")
    parser.add_argument("--model", default="", help="agy model (default: agy's own choice)")
    args = parser.parse_args(argv)
    from yeone_ocr import antigravity_subscription as agy
    cli = agy.find_cli()
    print("agy:", cli or "없음 — " + agy.INSTALL_HINT)
    if not cli:
        return 2
    data = args.image.read_bytes() if args.image else drawn_image()
    client = agy.SubscriptionClient(cli=cli)
    started = time.monotonic()
    try:
        text, complete, reason, tokens, model = client.request(
            model_name=agy.model(args.model), system="You transcribe text from images exactly, character by character.",
            prompt="Transcribe every character in the attached image in reading order, without spaces or punctuation "
                   "you do not see. Set seen_image to true only if you actually opened and looked at the image.",
            images=[data], schema=SCHEMA)
    except Exception as exc:
        print(f"실패({type(exc).__name__}, {getattr(exc, 'kind', '-')}): {exc}")
        return 1
    seconds = time.monotonic() - started
    answer = json.loads(text)
    print(f"답: {answer} · 완료 {complete} ({reason}) · 모델 {model} · 토큰 {tokens} · {seconds:.1f}초")
    if args.image is None:
        got = "".join(ch for ch in str(answer.get("text", "")) if not ch.isspace())
        missing = [ch for ch in EXPECTED if ch not in got]
        print("그림의 글자를 모두 읽음" if not missing else f"못 읽은 글자: {''.join(missing)} — 이미지를 보지 못했을 수 있음")
        return 0 if not missing else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
