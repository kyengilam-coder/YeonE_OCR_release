"""창 프로그램의 단추·체크 상자·카드·진행 막대 모양을 그린다.

tkinter 의 기본 clam 테마는 모서리가 각지고 체크 상자가 ☒(가위표)로 보인다. 여기서 둥근 모서리와 ✓ 표시를
알파 채널이 있는 PNG 로 직접 그려 ttk 이미지 요소(9칸 늘이기)로 쓴다. 외부 패키지(PIL) 없이 표준 라이브러리만 쓰므로
패키지를 설치하기 전에도 창이 뜬다. 모서리는 4×4 표본으로 계산해 부드럽게(안티에일리어싱) 그린다.

scale 은 화면 배율(Tk 의 1인치 픽셀 수 / 96)이다. 고해상도 화면에서도 같은 크기로 보이게 모든 치수에 곱한다.

주의: ttk 이미지 요소는 가운데 부분을 늘이지 않고 타일처럼 되풀이해 붙인다. 가운데가 1픽셀이면 넓은 카드 하나에
수십만 번 그려 창이 멈춘 것처럼 느려지므로, 늘어나는 그림은 가운데를 넉넉히(stretch_w·stretch_h) 둔다.
"""
from __future__ import annotations
import base64, math, struct, zlib

RGBA = tuple[int, int, int, int]


def hex_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def png_b64(w: int, h: int, rows: list[list[RGBA]]) -> str:
    """RGBA 픽셀 행렬 → PNG(base64). tk.PhotoImage(data=…) 로 바로 쓴다(Tk 8.6 은 PNG 알파를 지원)."""
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def _in_rrect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float, corners: str = "lrtb") -> bool:
    """점이 둥근 사각형 안인가. corners 에 든 모서리만 둥글다(l·r 은 왼·오른쪽 두 모서리, 'lrtb' 는 넷 다)."""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    if r <= 0:
        return True
    left, right = "l" in corners, "r" in corners
    cx = x0 + r if (x < x0 + r and left) else (x1 - r if (x > x1 - r and right) else None)
    cy = y0 + r if y < y0 + r else (y1 - r if y > y1 - r else None)
    if cx is None or cy is None:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy or 1.0)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _render(w: int, h: int, color_at, ss: int = 4, solid: list[tuple[float, float, float, float, RGBA]] | None = None,
            flat: int = 0) -> list[list[RGBA]]:
    """표본점마다 color_at(x, y) → (r, g, b, a) 또는 None(투명)을 물어 평균낸다(미리 곱한 알파로 섞음).
    solid 의 사각형(x0, y0, x1, y1, 색) 안에 통째로 드는 픽셀은 표본 없이 그 색으로 칠한다(넓은 그림을 빨리 그리려고).
    flat > 0 이면 둥근 모서리에서 flat 픽셀 넘게 떨어진 곧은 변 구간은 한 칸만 계산해 되풀이한다
    (그 구간은 가로로는 열마다, 세로로는 행마다 모양이 같다)."""
    rows: list[list[RGBA]] = []
    n = ss * ss
    solid = solid or []
    fx0, fx1 = (flat, w - flat) if flat and w - 2 * flat > 1 else (w, w)     # 같은 열이 되풀이되는 구간 [fx0, fx1)
    fy0, fy1 = (flat, h - flat) if flat and h - 2 * flat > 1 else (h, h)
    for j in range(h):
        if fy0 < j < fy1:
            rows.append(list(rows[fy0])); continue
        row: list[RGBA | None] = [None] * w
        for x0, y0, x1, y1, c in solid:                       # 사각형 안에 통째로 드는 칸은 한 번에(행 조각 채우기)
            if y0 <= j and j + 1 <= y1:
                a0, a1 = max(0, math.ceil(x0)), min(w, math.floor(x1))
                if a1 > a0:
                    row[a0:a1] = [c] * (a1 - a0)
        for i in range(w):
            if row[i] is not None:
                continue
            if fx0 < i < fx1:
                row[i] = row[fx0]; continue
            r = g = b = a = 0.0
            for sy in range(ss):
                y = j + (sy + 0.5) / ss
                for sx in range(ss):
                    c = color_at(i + (sx + 0.5) / ss, y)
                    if c is not None:
                        al = c[3] / 255.0
                        r += c[0] * al; g += c[1] * al; b += c[2] * al; a += al
            if a <= 0:
                row[i] = (0, 0, 0, 0)
            else:
                row[i] = (round(r / a), round(g / a), round(b / a), round(255 * a / n))
        rows.append(row)                                       # type: ignore[arg-type]
    return rows


def stretch_size(radius: int, stretch: int) -> tuple[int, int]:
    """9칸 늘이기 그림의 크기와 테두리 폭: 모서리(radius+1) 두 개 + 늘어나는 가운데(stretch)."""
    b = radius + 1
    return 2 * b + stretch, b


def rounded_box(w: int, h: int, radius: float, fill: str | None, border: str | None = None, bw: float = 1.0,
                corners: str = "lrtb", ring: str | None = None, ring_w: float = 0.0) -> str:
    """둥근 사각형(단추·카드·막대). border 는 테두리 색(bw 두께), ring 은 바깥쪽 초점 고리(ring_w 두께, 테두리 바깥)."""
    f = hex_rgb(fill) + (255,) if fill else None
    bd = hex_rgb(border) + (255,) if border else None
    rg = hex_rgb(ring) + (255,) if ring else None
    o = ring_w if ring else 0.0

    def at(x: float, y: float):
        if rg and _in_rrect(x, y, 0, 0, w, h, radius + o, corners) and not _in_rrect(x, y, o, o, w - o, h - o, radius, corners):
            return rg
        if not _in_rrect(x, y, o, o, w - o, h - o, radius, corners):
            return None
        if bd and not _in_rrect(x, y, o + bw, o + bw, w - o - bw, h - o - bw, max(0.0, radius - bw), corners):
            return bd
        return f

    solid = []
    if f:                                                     # 모서리·테두리에서 떨어진 안쪽(십자 모양 두 사각형)은 표본 없이 채운다
        e = o + (bw if bd else 0.0) + 1
        solid = [(e + radius, e, w - e - radius, h - e, f), (e, e + radius, w - e, h - e - radius, f)]
    return png_b64(w, h, _render(w, h, at, solid=solid, flat=math.ceil(o + radius + bw)))


def checkbox(size: int, on: bool, accent: str, border: str, fill: str = "#ffffff", mark: str = "#ffffff",
             gap: int = 0, hover: bool = False, disabled: bool = False, ring: str | None = None) -> str:
    """체크 상자. 켜지면 강조색으로 채운 상자에 흰 ✓, 꺼지면 흰 상자에 회색 테두리. gap 은 오른쪽 여백(글자와의 간격).
    ring 은 초점 고리 색: 상자 가장자리 안쪽에 두르므로 크기가 바뀌지 않고, 켜진 상자의 채움·✓ 대비도 그대로다."""
    s = float(size)
    r = s * 0.22
    bw = max(1.0, s / 14)
    acc = hex_rgb("#c9c0b2" if disabled else accent) + (255,)
    bd = hex_rgb("#d9d2c6" if disabled else (accent if hover else border)) + (255,)
    fl = hex_rgb("#f4f1ec" if disabled else fill) + (255,)
    mk = hex_rgb(mark) + (255,)
    rg = hex_rgb(ring) + (255,) if ring else None
    rw = bw * 1.8
    pts = [(0.25 * s, 0.53 * s), (0.43 * s, 0.70 * s), (0.76 * s, 0.33 * s)]
    sw = max(1.6, s * 0.13)

    def at(x: float, y: float):
        if x > s or not _in_rrect(x, y, 0, 0, s, s, r):
            return None
        if rg and not _in_rrect(x, y, rw, rw, s - rw, s - rw, max(0.0, r - rw)):
            return rg
        if on:
            d = min(_seg_dist(x, y, *pts[0], *pts[1]), _seg_dist(x, y, *pts[1], *pts[2]))
            return mk if d <= sw / 2 else acc
        if not _in_rrect(x, y, bw, bw, s - bw, s - bw, max(0.0, r - bw)):
            return bd
        return fl

    return png_b64(size + gap, size, _render(size + gap, size, at))


def radio(size: int, on: bool, accent: str, border: str, fill: str = "#ffffff", gap: int = 0,
          hover: bool = False, disabled: bool = False, ring: str | None = None) -> str:
    """라디오 단추. 켜지면 강조색 고리 안에 강조색 점. ring 은 초점 고리 색(바깥 테두리를 그 색으로)."""
    s = float(size)
    c = s / 2
    bw = max(1.0, s / 14) * (1.6 if (on or ring) else 1.0)
    acc = hex_rgb("#c9c0b2" if disabled else accent) + (255,)
    bd = hex_rgb("#d9d2c6" if disabled else (ring or (accent if (hover or on) else border))) + (255,)
    fl = hex_rgb("#f4f1ec" if disabled else fill) + (255,)

    def at(x: float, y: float):
        d = math.hypot(x - c, y - c)
        if x > s or d > c:
            return None
        if d > c - bw:
            return bd
        if on and d <= s * 0.24:
            return acc
        return fl

    return png_b64(size + gap, size, _render(size + gap, size, at))


def chevron(w: int, h: int, color: str, stroke: float = 1.6) -> str:
    """아래 꺾쇠(⌄) — 고르기 칸의 펼침 표시."""
    c = hex_rgb(color) + (255,)
    a, m, b = (w * 0.18, h * 0.32), (w * 0.5, h * 0.70), (w * 0.82, h * 0.32)

    def at(x: float, y: float):
        return c if min(_seg_dist(x, y, *a, *m), _seg_dist(x, y, *m, *b)) <= stroke / 2 else None

    return png_b64(w, h, _render(w, h, at))
