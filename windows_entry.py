"""PyInstaller entry point. Keep this file free of application imports."""
import multiprocessing


if __name__ == "__main__":
    # Required before importing GUI/SDK modules in a frozen Windows process.
    multiprocessing.freeze_support()
    from yeone_ocr.windows_app import main
    raise SystemExit(main())
