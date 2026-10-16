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
                corners: str = "lrtb", ring: str | None = None, ring_w: float = 0.0, ring_gap: float = 0.0) -> str:
    """둥근 사각형(단추·카드·막대). border 는 테두리 색(bw 두께), ring 은 바깥쪽 고리(ring_w 두께, 테두리 바깥에서 ring_gap 만큼 띄움 — 틈은 투명).
    ring 이 없어도 ring_w·ring_gap 을 주면 그 자리를 비워 둔다(기본 단추가 바뀌어도 단추끼리 크기가 같게)."""
    f = hex_rgb(fill) + (255,) if fill else None
    bd = hex_rgb(border) + (255,) if border else None
    rg = hex_rgb(ring) + (255,) if ring else None
    o = ring_w + ring_gap if (ring or ring_gap) else 0.0

    def at(x: float, y: float):
        if rg and _in_rrect(x, y, 0, 0, w, h, radius + o, corners) and not _in_rrect(x, y, ring_w, ring_w, w - ring_w, h - ring_w, radius + ring_gap, corners):
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


def underline_box(w: int, h: int, fill: str, bar: str | None, bar_h: float) -> str:
    """밑줄 탭의 판: fill 로 채우고 아래 bar_h 픽셀에 bar 색 막대(고른 탭). bar 가 없으면 판만(고르지 않은 탭)."""
    f = hex_rgb(fill) + (255,)
    b = hex_rgb(bar) + (255,) if bar else f
    edge = h - bar_h
    return png_b64(w, h, [[b if j >= edge else f] * w for j in range(h)])


def _icon(size: int, color: str, stroke: float, paths: list[list[tuple[float, float]]], extra=None) -> str:
    """16단위 격자에 그린 선 그림(시안의 svg 와 같은 좌표)을 size 픽셀로. paths 는 꺾은선(점 목록), extra(x, y) 는 그 밖의 모양(거리 → 선 위인가)."""
    k = size / 16.0
    c = hex_rgb(color) + (255,)
    segs = [(a[0] * k, a[1] * k, b[0] * k, b[1] * k) for p in paths for a, b in zip(p, p[1:])]
    half = stroke * k / 2

    def at(x: float, y: float):
        if any(_seg_dist(x, y, *s) <= half for s in segs):
            return c
        if extra is not None and extra(x / k, y / k, stroke / 2):
            return c
        return None

    return png_b64(size, size, _render(size, size, at))


def receipt(size: int, color: str, stroke: float = 1.4) -> str:
    """영수증(돈이 드는 단계): 아래가 톱니인 세로 종이 + 줄 셋. 시안 gen_common.receipt 와 같은 모양."""
    paper = [(3.5, 1.5), (12.5, 1.5), (12.5, 14.5), (11, 13.5), (9.5, 14.5), (8, 13.5), (6.5, 14.5), (5, 13.5), (3.5, 14.5), (3.5, 1.5)]
    return _icon(size, color, stroke, [paper, [(6, 5.5), (10, 5.5)], [(6, 8.5), (10, 8.5)], [(6, 11), (8.5, 11)]])


def lock(size: int, color: str, stroke: float = 1.6) -> str:
    """자물쇠(실행 중 잠김): 둥근 몸통 + 위 고리."""
    def shape(x: float, y: float, half: float) -> bool:
        body = _in_rrect(x, y, 3 - half, 7 - half, 13 + half, 14.5 + half, 1.5 + half) and not _in_rrect(x, y, 3 + half, 7 + half, 13 - half, 14.5 - half, max(0.0, 1.5 - half))
        arc = y <= 5 and abs(math.hypot(x - 8, y - 5) - 2.5) <= half
        return body or arc
    return _icon(size, color, stroke, [[(5.5, 7), (5.5, 5)], [(10.5, 7), (10.5, 5)]], extra=shape)


def seal(size: int, fill: str, line: str, large: bool = True, scale: float = 1.0) -> str:
    """도장 바탕: 인주색 둥근 사각(반지름 4, 작은 도장은 3) + 안쪽 가는 테(큰 도장은 3px, 작은 것은 2px 들여). 글자는 창이 그 위에 쓴다.
    size 는 픽셀(배율을 곱한 값), large 는 48px 도장인가(34px 이하는 작은 도장)."""
    s = float(size)
    r = (4.0 if large else 3.0) * scale
    inset = (3.0 if large else 2.0) * scale
    lw = max(1.0, scale)
    f = hex_rgb(fill) + (255,)
    ln = hex_rgb(line) + (255,)
    ri = 2.0 * scale

    def at(x: float, y: float):
        if not _in_rrect(x, y, 0, 0, s, s, r):
            return None
        if _in_rrect(x, y, inset, inset, s - inset, s - inset, ri) and not _in_rrect(x, y, inset + lw, inset + lw, s - inset - lw, s - inset - lw, max(0.0, ri - lw)):
            return ln
        return f

    return png_b64(size, size, _render(size, size, at))


def folder(size: int, color: str, stroke: float = 1.4) -> str:
    """폴더(원본 경로 줄 · [저장 폴더])."""
    return _icon(size, color, stroke, [[(1.5, 4.5), (1.5, 13), (14.5, 13), (14.5, 5.5), (7.5, 5.5), (6, 3.5), (1.5, 3.5), (1.5, 4.5)]])


def file_doc(size: int, color: str, stroke: float = 1.4) -> str:
    """파일(설정 탭 양식 HWPX 이름 앞): 오른쪽 위 귀가 접힌 종이. 시안 gen_common.filei 와 같은 모양."""
    return _icon(size, color, stroke, [[(3.5, 1.5), (9.5, 1.5), (12.5, 4.5), (12.5, 14.5), (3.5, 14.5), (3.5, 1.5)], [(9.5, 1.5), (9.5, 4.5), (12.5, 4.5)]])


def check(size: int, color: str, stroke: float = 2.0) -> str:
    """✓(저장됨)."""
    return _icon(size, color, stroke, [[(3, 8.5), (6, 11.5), (13, 4)]])


def clip(size: int, color: str, stroke: float = 1.5) -> str:
    """클립보드(빈 이미지 상자의 [이미지 붙여넣기]): 둥근 판 + 위 집게 + 줄 둘."""
    def board(x: float, y: float, half: float) -> bool:
        return (_in_rrect(x, y, 3 - half, 3 - half, 13 + half, 14.5 + half, 1.5 + half)
                and not _in_rrect(x, y, 3 + half, 3 + half, 13 - half, 14.5 - half, max(0.0, 1.5 - half)))
    return _icon(size, color, stroke, [[(6, 3), (6, 1.8), (10, 1.8), (10, 3)], [(5.8, 7.5), (10.2, 7.5)], [(5.8, 10.5), (8.8, 10.5)]], extra=board)


def expand(size: int, color: str, stroke: float = 1.5) -> str:
    """두 번 눌러 크게 보기(원본 열기) 표지: 바깥으로 벌어지는 두 화살."""
    return _icon(size, color, stroke, [[(9.5, 2.5), (13.5, 2.5), (13.5, 6.5)], [(13.5, 2.5), (8.5, 7.5)],
                                       [(6.5, 13.5), (2.5, 13.5), (2.5, 9.5)], [(2.5, 13.5), (7.5, 8.5)]])


def step_circle(size: int, kind: str, ink: str, edge: str, red: str, panel: str, scale: float = 1.0) -> str:
    """단계 띠의 동그라미(지름 size — 시안 26px 에 배율을 곱한 값). 글자(번호·'–'·'!')는 창이 그림 위에 쓴다(Label compound='center').
    done = 먹 채움 + 흰 ✓ · now = 먹 2px 고리 + 가운데 10px 먹 점 · wait = EDGE 1.5px 고리 · skip = EDGE 1.5px 점선 고리(손으로 그은 조각 — 3 긋고 2 띄움)
    · fail = PANEL 속 + RED 2px 고리(채우지 않음 — 인주 채움과 갈리게) · stop = 먹 2px 고리 + 가운데 8px 먹 네모."""
    s = float(size); c = s / 2
    k = s / 26.0
    INK, EDGE, RED, PAN = (hex_rgb(v) + (255,) for v in (ink, edge, red, panel))
    WHITE = (255, 255, 255, 255)
    ring = {"now": (INK, 2.0), "wait": (EDGE, 1.5), "skip": (EDGE, 1.5), "fail": (RED, 2.0), "stop": (INK, 2.0)}
    check = [(c + (x - 8) * k * 14 / 16, c + (y - 8) * k * 14 / 16) for x, y in ((3.5, 8.5), (6.5, 11.5), (12.5, 4.5))]
    sw = 2.2 * k * 14 / 16
    period, on = 5.0 * k, 3.0 * k                                   # 점선: 둘레를 따라 3 긋고 2 띄움
    n = max(1, round(math.pi * (s - 1.5 * k) / period))
    period = math.pi * (s - 1.5 * k) / n                           # 둘레에 꼭 맞게(이음매에서 조각이 겹치지 않게)

    def at(x: float, y: float):
        d = math.hypot(x - c, y - c)
        if d > c:
            return None
        if kind == "done":
            dd = min(_seg_dist(x, y, *check[0], *check[1]), _seg_dist(x, y, *check[1], *check[2]))
            return WHITE if dd <= sw / 2 else INK
        col, w = ring.get(kind, (EDGE, 1.5))
        w *= k
        if d > c - w:
            if kind == "skip":
                pos = (math.atan2(y - c, x - c) + math.pi) * (c - w / 2)
                return col if pos % period < on else PAN
            return col
        if kind == "now" and d <= 5.0 * k:
            return INK
        if kind == "stop" and abs(x - c) <= 4.0 * k and abs(y - c) <= 4.0 * k:
            return INK
        return PAN

    return png_b64(size, size, _render(size, size, at))
