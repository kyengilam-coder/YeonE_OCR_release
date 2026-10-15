"""Image input preview. Clipboard access only occurs after an explicit paste action."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def preview_image(path: str, size: tuple[int, int]):
    """Return an oriented thumbnail, including the first page of bundled images."""
    from PIL import Image, ImageOps
    if Path(path).suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(path) as document:
            page = document[0]
            scale = min(size[0] / page.rect.width, size[1] / page.rect.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    with Image.open(path) as source:
        source.seek(0)
        source.draft("RGB", size)
        oriented = ImageOps.exif_transpose(source)
        try:
            oriented.thumbnail(size, Image.Resampling.LANCZOS)
            return oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()


class ImageInputPreview(ttk.Frame):
    def __init__(self, parent, *, variable, paste, open_source, height=180):
        super().__init__(parent, style="Panel.TFrame")
        self.variable = variable
        self._photo = None
        self._thumbnail = None
        self._pending = None
        self._path = None
        self._stamp = None
        self.canvas = tk.Canvas(self, height=height, background="#faf8f3", takefocus=True,
                                highlightthickness=1, highlightbackground="#c9c2b5",
                                highlightcolor="#73513d", borderwidth=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        self.path_label = ttk.Label(self, textvariable=variable, style="Hint.TLabel")
        self.path_label.pack(fill="x", pady=(3, 0))
        self.canvas.bind("<Button-1>", lambda _e: self.canvas.focus_set())
        self.canvas.bind("<Double-Button-1>", lambda _e: open_source() if variable.get() else None)
        self.canvas.bind("<Control-v>", paste)
        self.canvas.bind("<Control-V>", paste)
        self.canvas.bind("<<Paste>>", paste)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="이미지 붙여넣기 (Ctrl+V)", command=paste)
        menu.add_command(label="원본 크게 보기", command=open_source)
        self.canvas.bind("<Button-3>", lambda e: self._popup(menu, e))
        self.canvas.bind("<Configure>", self._schedule)
        self._trace = variable.trace_add("write", self._schedule)
        self.bind("<Destroy>", self._destroyed, add="+")
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

    def _draw(self):
        self._pending = None
        canvas = self.canvas
        width, height = max(100, canvas.winfo_width()), max(60, canvas.winfo_height())
        self.path_label.configure(wraplength=width)
        canvas.delete("all")
        self._photo = None
        path = self.variable.get().strip()
        try:
            stat = Path(path).stat() if path else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat else None
        except OSError:
            stamp = None
        if path != self._path or stamp != self._stamp:
            if self._thumbnail is not None:
                self._thumbnail.close()
            self._thumbnail = None
            self._path = path
            self._stamp = stamp
            if path:
                try:
                    self._thumbnail = preview_image(path, (1800, 1000))
                except Exception:
                    # Keep the source selected so opening it or OCR can explain the error.
                    pass
        if self._thumbnail is not None:
            from PIL import Image, ImageTk
            shown = self._thumbnail.copy()
            shown.thumbnail((max(1, width - 16), max(1, height - 16)), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(shown, master=canvas)
            shown.close()
            canvas.create_image(width // 2, height // 2, image=self._photo)
        else:
            text = ("미리보기를 불러올 수 없습니다. 이미지 고르기로 다시 선택하십시오." if path
                    else "캡처한 이미지를 여기에 붙여넣으세요\n\n클릭 후 Ctrl+V · 마우스 오른쪽 버튼으로 붙여넣기")
            canvas.create_text(width // 2, height // 2, text=text, fill="#8b857a",
                               width=max(80, width - 32), justify="center")

    def _destroyed(self, event):
        if event.widget is not self:
            return
        self.variable.trace_remove("write", self._trace)
        if self._pending is not None:
            self.after_cancel(self._pending)
        if self._thumbnail is not None:
            self._thumbnail.close()
