"""문서·스캔 앱 아이콘의 SVG/PNG/ICO를 재생성한다. 외부 패키지 불필요.

저장소 루트에서 python tools/build_app_icon.py. GUI는 생성된 파일만 읽는다.
"""
from __future__ import annotations
import base64
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from yeone_ocr.gui_art import _in_rrect, _render, _seg_dist, hex_rgb, png_b64

DEST = ROOT / "yeone_ocr" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
RED, PAPER, GOLD, INK = "#B7382D", "#FFFAEB", "#FFCF70", "#862B24"

SVG = f'''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 96 96">
  <title>연어OCR — 문서 판독</title>
  <rect x="3" y="3" width="90" height="90" rx="21" fill="{RED}"/>
  <path d="M30 19H58L70 31V73Q70 77 66 77H30Q26 77 26 73V23Q26 19 30 19Z" fill="{PAPER}"/>
  <path d="M58 19V31H70Z" fill="{GOLD}"/>
  <path d="M35 43.5H59M35 55.5H59M35 67.5H49" fill="none" stroke="{INK}" stroke-width="5" stroke-linecap="round"/>
  <path d="M15 28V15H28M68 15H81V28M81 68V81H68M28 81H15V68" fill="none" stroke="{GOLD}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
'''


def pixels(size):
    red, paper, gold, ink = [hex_rgb(c) + (255,) for c in (RED, PAPER, GOLD, INK)]
    brackets = (((15, 28), (15, 15), (28, 15)), ((68, 15), (81, 15), (81, 28)),
                ((81, 68), (81, 81), (68, 81)), ((28, 81), (15, 81), (15, 68)))
    # 제목 표시줄의 16/20px에는 줄을 둘로 줄여 획 사이 빈 공간을 확보한다.
    lines = ((35, 46, 59, 46), (35, 62, 53, 62)) if size <= 20 else (
        (35, 43.5, 59, 43.5), (35, 55.5, 59, 55.5), (35, 67.5, 49, 67.5))

    def at(x, y):
        x, y = x * 96 / size, y * 96 / size
        if not _in_rrect(x, y, 3, 3, 93, 93, 21):
            return None
        for a, b, c in brackets:
            if min(_seg_dist(x, y, *a, *b), _seg_dist(x, y, *b, *c)) <= 2.5:
                return gold
        if _in_rrect(x, y, 26, 19, 70, 77, 4) and y >= x - 39:
            if x >= 58 and y <= 31:
                return gold
            if any(_seg_dist(x, y, *line) <= 2.5 for line in lines):
                return ink
            return paper
        return red

    return _render(size, size, at, ss=4 if size <= 64 else 3)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "app-icon.svg").write_text(SVG, encoding="utf-8")
    frames = []
    for size in SIZES:
        data = base64.b64decode(png_b64(size, size, pixels(size)))
        (DEST / f"app-icon-{size}.png").write_bytes(data)
        frames.append((size, data))
    # Vista 이후 Windows가 지원하는 PNG 포함 ICO. 해상도별 원본을 직접 넣는다.
    offset = 6 + 16 * len(frames)
    directory = []
    for size, data in frames:
        directory.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    icon = struct.pack("<HHH", 0, 1, len(frames)) + b"".join(directory) + b"".join(data for _, data in frames)
    (DEST / "YeonE_OCR.ico").write_bytes(icon)
    print(f"Saved SVG, {len(frames)} PNG sizes, and ICO to {DEST}")


if __name__ == "__main__":
    main()
