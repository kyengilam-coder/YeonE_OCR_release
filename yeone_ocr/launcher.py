"""소스·Windows 설치판의 공통 실행기. 검사와 CLI 는 Tk 를 불러오기 전에 분기한다."""
from __future__ import annotations

def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    from .windows_app import main as windows_main
    return windows_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
