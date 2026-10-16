"""Image input preview. Clipboard access only occurs after an explicit paste action."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def preview_page_count(path: str) -> int:
    """Count source pages without retaining an open file or loading every image."""
    if Path(path).suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(path) as document:
            return document.page_count
    from PIL import Image
    with Image.open(path) as source:
        # MPO may contain HDR gain maps or previews; OCR uses only its main picture.
        return 1 if source.format == "MPO" else (getattr(source, "n_frames", 1) or 1)


def preview_image(path: str, size: tuple[int, int], page_index: int = 0):
    """Return an oriented thumbnail of a zero-based image or document page."""
    from PIL import Image, ImageOps
    if page_index < 0:
        raise IndexError("미리보기 쪽 번호가 범위를 벗어났습니다.")
    if Path(path).suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(path) as document:
            if page_index >= document.page_count:
                raise IndexError("미리보기 쪽 번호가 범위를 벗어났습니다.")
            page = document[page_index]
            scale = min(size[0] / page.rect.width, size[1] / page.rect.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    with Image.open(path) as source:
        if source.format == "MPO" and page_index:
            raise IndexError("미리보기 쪽 번호가 범위를 벗어났습니다.")
        try:
            source.seek(page_index)
        except EOFError as exc:
            raise IndexError("미리보기 쪽 번호가 범위를 벗어났습니다.") from exc
        source.draft("RGB", size)
        oriented = ImageOps.exif_transpose(source)
        try:
            oriented.thumbnail(size, Image.Resampling.LANCZOS)
            return oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()


def rounded_path(x0: float, y0: float, x1: float, y1: float, r: float, steps: int = 6) -> list[tuple[float, float]]:
    """둥근 사각형 둘레의 점(시계 방향, 처음 점으로 닫힘) — 판 채우기·실선·손으로 그은 점선이 같은 길을 쓴다."""
    import math
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts: list[tuple[float, float]] = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for k in range(steps + 1):
            a = math.radians(a0 + 90 * k / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts


def dash_segments(pts: list[tuple[float, float]], on: float, off: float) -> list[tuple[float, ...]]:
    """꺾은선을 따라 on 만큼 긋고 off 만큼 띄운 조각들(각 조각은 꺾은선 좌표). Tk 의 dash 는 Windows 에서 몇 가지 펜 모양으로만 그려져 쓰지 않는다."""
    import math
    out: list[tuple[float, ...]] = []
    cur: list[float] = []
    drawing, left = True, on
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = math.hypot(bx - ax, by - ay); t = 0.0
        while seg > 0 and t < seg:
            step = min(left, seg - t)
            x, y = ax + (bx - ax) * t / seg, ay + (by - ay) * t / seg
            nx, ny = ax + (bx - ax) * (t + step) / seg, ay + (by - ay) * (t + step) / seg
            if drawing:
                if not cur:
                    cur = [x, y]
                cur += [nx, ny]
            t += step; left -= step
            if left <= 1e-6:
                if drawing and len(cur) >= 4:
                    out.append(tuple(cur))
                cur = []; drawing = not drawing; left = on if drawing else off
    if drawing and len(cur) >= 4:
        out.append(tuple(cur))
    return out


class ImageInputPreview(ttk.Frame):
    """이미지 탭의 큰 원본 상자(디자인 명세 §5.10). 누르면 초점 → Ctrl+V, 오른쪽 버튼 메뉴, 두 번 누르면 원본을 크게 본다(클립보드는 붙여넣기를 할 때만 읽는다).
    빈 상자: QUIET 판 + EDGE 1.5 점선(손으로 그은 조각) + 가운데 empty_widget(창이 넣는 먹 단추 [이미지 붙여넣기]) + 안내 두 줄.
    찬 상자: FIELD 판 + LINE 1 테두리, 그림을 8px 여백 안 가운데로, 오른쪽 아래 '두 번 눌러 크게 보기'. 초점이면 테두리 INK 2px.
    상자 아래 쪽 넘기기 줄([◀ 이전] '현재 / 전체쪽' [다음 ▶]) — 붙여넣어 뒤에 쪽이 붙으면 show_last_page 로 마지막 쪽을 보인다.
    경로는 상자에 적지 않는다(책 카드 제목 줄의 파일 이름·풍선과 결과 저장 칸이 대신)."""
    COLORS = {"PANEL": "#fcfaf5", "QUIET": "#f6f1e6", "FIELD": "#ffffff", "LINE": "#e2d9c8", "EDGE": "#8f8271", "INK": "#221c18", "MUTED": "#574d42"}
    EMPTY_LINES = ("Win+Shift+S로 캡처한 이미지를 여기에 붙여넣으세요",
                   "상자를 누르고 Ctrl+V · 오른쪽 버튼 메뉴로도 · 계속 붙이면 뒤에 쪽이 붙음")
    FOCUS_LINE = "Ctrl+V를 누르면 붙여 넣습니다"
    UNREADABLE = "미리보기를 불러올 수 없습니다. 이미지 고르기로 다시 선택하십시오."
    ZOOM_HINT = "두 번 눌러 크게 보기"

    def __init__(self, parent, *, variable, paste, open_source, height=168, colors=None, fonts=None, scale: float = 1.0, zoom_icon=None):
        super().__init__(parent, style="Panel.TFrame")
        self.variable = variable
        self.C = dict(self.COLORS, **(colors or {}))
        self.fonts = fonts or {}
        self.scale = max(1.0, float(scale or 1.0))
        self.zoom_icon = zoom_icon
        self.empty_widget = None
        self._photo = None
        self._thumbnail = None
        self._thumbnail_page = None
        self._pending = None
        self._path = None
        self._stamp = None
        self._page_index = 0
        self._page_count = 0
        self._requested_page = None
        self.canvas = tk.Canvas(self, height=height, background=self.C["PANEL"], takefocus=True,
                                highlightthickness=0, borderwidth=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        # 쪽 넘기기 줄(상자 아래): [◀ 이전] '현재 / 전체쪽' [다음 ▶] — 여러 장을 붙이거나 묶으면 모든 쪽을 넘겨 본다
        # 원본이 없으면(0쪽) 줄째 숨긴다 — 빈 상자는 가운데 안내가 대신한다
        navigation = self.navigation = ttk.Frame(self, style="Panel.TFrame")
        self.previous_button = ttk.Button(navigation, text="◀ 이전", width=8, style="Small.TButton", cursor="hand2",
                                          command=lambda: self._change_page(-1))
        self.previous_button.pack(side="left")
        self.page_label = ttk.Label(navigation, text="0쪽", anchor="center", style="Hint.TLabel")
        self.page_label.pack(side="left", fill="x", expand=True)
        self.next_button = ttk.Button(navigation, text="다음 ▶", width=8, style="Small.TButton", cursor="hand2",
                                      command=lambda: self._change_page(1))
        self.next_button.pack(side="right")
        self._update_navigation()
        self.canvas.bind("<Button-1>", lambda _e: self.canvas.focus_set())
        self.canvas.bind("<Double-Button-1>", lambda _e: open_source() if variable.get() else None)
        self.canvas.bind("<Control-v>", paste)
        self.canvas.bind("<Control-V>", paste)
        self.canvas.bind("<<Paste>>", paste)
        self.canvas.bind("<FocusIn>", self._schedule, add="+")
        self.canvas.bind("<FocusOut>", self._schedule, add="+")
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="이미지 붙여넣기 (Ctrl+V)", command=paste)
        menu.add_command(label="원본 크게 보기", command=open_source)
        self.canvas.bind("<Button-3>", lambda e: self._popup(menu, e))
        self.canvas.bind("<Configure>", self._schedule)
        self._trace = variable.trace_add("write", self._schedule)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._schedule()

    def px(self, v: float) -> int:
        return max(1, int(round(v * self.scale)))

    def set_empty_widget(self, widget) -> None:
        """빈 상자 가운데에 둘 위젯(canvas 의 자식 — 창이 만든 [이미지 붙여넣기] 단추)."""
        self.empty_widget = widget
        self._schedule()

    def _popup(self, menu, event):
        self.canvas.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _schedule(self, *_):
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(80, self._draw)

    def _focused(self) -> bool:
        try:
            return self.canvas.focus_get() is self.canvas
        except (KeyError, tk.TclError):
            return False

    def _frame(self, w: int, h: int, empty: bool, focused: bool) -> None:
        """판과 테두리: 빈 상자는 QUIET + EDGE 점선, 찬 상자는 FIELD + LINE 실선, 초점이면 INK 2px 실선."""
        C, P, cv = self.C, self.px, self.canvas
        lw = P(2) if focused else (1.5 * self.scale if empty else max(1, P(1)))
        inset = lw / 2
        pts = rounded_path(inset, inset, w - inset, h - inset, P(6))
        cv.create_polygon([c for pt in pts for c in pt], fill=C["QUIET"] if empty else C["FIELD"], outline="")
        color = C["INK"] if focused else (C["EDGE"] if empty else C["LINE"])
        if empty and not focused:
            for seg in dash_segments(pts, 3 * self.scale, 2 * self.scale):
                cv.create_line(*seg, fill=color, width=lw, capstyle="butt")
        else:
            cv.create_line([c for pt in pts for c in pt], fill=color, width=lw, joinstyle="round")

    def show_last_page(self):
        """Show the newest page after a source update, including a pending redraw."""
        self._requested_page = -1
        self._schedule()

    def _change_page(self, offset):
        page_index = max(0, min(self._page_count - 1, self._page_index + offset))
        if page_index != self._page_index:
            self._page_index = page_index
            self._update_navigation()
            self._schedule()

    def _update_navigation(self):
        if self._page_count and not self.navigation.winfo_manager():
            self.navigation.pack(fill="x", pady=(self.px(6), 0))
        elif not self._page_count:
            self.navigation.pack_forget()
        self.page_label.configure(text=(f"{self._page_index + 1} / {self._page_count}쪽"
                                        if self._page_count else "0쪽"))
        self.previous_button.configure(state="normal" if self._page_index > 0 else "disabled")
        self.next_button.configure(state=("normal" if self._page_index + 1 < self._page_count
                                         else "disabled"))

    def _clear_thumbnail(self):
        if self._thumbnail is not None:
            self._thumbnail.close()
        self._thumbnail = None
        self._thumbnail_page = None

    def _draw(self):
        self._pending = None
        canvas = self.canvas; C, P, F = self.C, self.px, self.fonts
        width, height = max(100, canvas.winfo_width()), max(60, canvas.winfo_height())
        canvas.delete("all")
        self._photo = None
        path = self.variable.get().strip()
        try:
            stat = Path(path).stat() if path else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat else None
        except OSError:
            stamp = None
        if path != self._path or stamp != self._stamp:
            self._clear_thumbnail()
            self._path = path
            self._stamp = stamp
            self._page_index = 0
            self._page_count = 0
            if path:
                try:
                    self._page_count = preview_page_count(path)
                except Exception:
                    # Keep the source selected so opening it or OCR can explain the error.
                    pass
        if self._requested_page is not None:
            self._page_index = max(0, self._page_count - 1)
            self._requested_page = None
        self._update_navigation()
        if self._page_count and self._thumbnail_page != self._page_index:
            self._clear_thumbnail()
            self._thumbnail_page = self._page_index
            try:
                self._thumbnail = preview_image(path, (1800, 1000), self._page_index)
            except Exception:
                # A damaged page should not prevent navigation to the other pages.
                pass
        focused = self._focused()
        self._frame(width, height, not path, focused)
        if self._thumbnail is not None:
            from PIL import Image, ImageTk
            shown = self._thumbnail.copy()
            shown.thumbnail((max(1, width - P(16)), max(1, height - P(16))), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(shown, master=canvas)
            shown.close()
            canvas.create_image(width // 2, height // 2, image=self._photo)
            # 오른쪽 아래 '두 번 눌러 크게 보기'(FIELD 띠 위)
            font = F.get("CAP", "TkSmallCaptionFont")
            t = canvas.create_text(width - P(6) - P(5), height - P(5) - P(2), text=self.ZOOM_HINT, anchor="se", fill=C["MUTED"], font=font)
            x0, y0, x1, y1 = canvas.bbox(t)
            if self.zoom_icon is not None:
                ic = canvas.create_image(x0 - P(4), (y0 + y1) // 2, image=self.zoom_icon, anchor="e")
                x0 = canvas.bbox(ic)[0]
            bg = canvas.create_rectangle(x0 - P(5), y0 - P(1), x1 + P(5), y1 + P(1), fill=C["FIELD"], outline="")
            canvas.tag_lower(bg, t)
            if self.zoom_icon is not None:
                canvas.tag_raise(ic)
        elif path:
            canvas.create_text(width // 2, height // 2, text=self.UNREADABLE, fill=C["MUTED"], font=F.get("BODY", "TkDefaultFont"),
                               width=max(80, width - P(32)), justify="center")
        else:
            lines = (self.EMPTY_LINES[0], self.FOCUS_LINE if focused else self.EMPTY_LINES[1])
            wrap = max(80, width - P(32))
            t1 = canvas.create_text(0, 0, text=lines[0], fill=C["INK"], font=F.get("BODY", "TkDefaultFont"), width=wrap, justify="center", anchor="n")
            t2 = canvas.create_text(0, 0, text=lines[1], fill=C["MUTED"], font=F.get("CAP", "TkSmallCaptionFont"), width=wrap, justify="center", anchor="n")
            h1 = canvas.bbox(t1)[3] - canvas.bbox(t1)[1]; h2 = canvas.bbox(t2)[3] - canvas.bbox(t2)[1]
            bw = self.empty_widget
            bh = bw.winfo_reqheight() if bw is not None else 0
            gap = P(8) if bw is not None else 0
            total = bh + gap + h1 + P(4) + h2
            top = max(P(8), (height - total) // 2)
            if bw is not None:
                canvas.create_window(width // 2, top, window=bw, anchor="n")
            canvas.coords(t1, width // 2, top + bh + gap)
            canvas.coords(t2, width // 2, top + bh + gap + h1 + P(4))

    def _destroyed(self, event):
        if event.widget is not self:
            return
        self.variable.trace_remove("write", self._trace)
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._clear_thumbnail()


def clip_x(pts: list[tuple[float, float]], x0: float, x1: float) -> list[tuple[float, float]]:
    """볼록한 다각형(rounded_path)을 x0 ≤ x ≤ x1 로 자른 점들 — 둥근 막대 안의 범위·채움을 막대 모양 그대로 그리려고."""
    def cut(points, x, keep_right):
        out = []
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            ina, inb = (ax >= x, bx >= x) if keep_right else (ax <= x, bx <= x)
            if ina:
                out.append((ax, ay))
            if ina != inb and bx != ax:
                t = (x - ax) / (bx - ax)
                out.append((x, ay + t * (by - ay)))
        return out
    return cut(cut(list(pts), x0, True), x1, False) if x1 > x0 else []
