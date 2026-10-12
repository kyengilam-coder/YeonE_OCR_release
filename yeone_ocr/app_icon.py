"""제목 표시줄·작업 표시줄의 전용 아이콘. GUI 시작 시 외부 패키지는 불필요하다."""
from pathlib import Path
import sys

ASSETS = Path(__file__).resolve().parent / "assets"
APP_ID = "YeonE.OCR.Desktop"  # 버전이 바뀌어도 작업 표시줄의 같은 앱으로 인식한다.


def set_windows_app_id():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass


def apply_window_icon(window):
    import tkinter as tk
    if sys.platform == "win32":
        try:
            window.iconbitmap(default=str(ASSETS / "YeonE_OCR.ico"))
            return
        except tk.TclError:
            pass
    try:
        window._app_icon_images = tuple(tk.PhotoImage(master=window, file=str(ASSETS / f"app-icon-{size}.png"))
                                        for size in (16, 32, 48, 64, 128, 256))
        window.iconphoto(True, *window._app_icon_images)
    except (tk.TclError, OSError):
        pass  # 아이콘이 누락된 배포에서도 본래 OCR 창은 열 수 있게 한다.
