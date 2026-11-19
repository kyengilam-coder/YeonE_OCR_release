"""Persistent, redacted process diagnostics for packaged GUI runs."""
import faulthandler
import os
from pathlib import Path
import threading
from datetime import datetime

_stream = None
_lock = threading.Lock()


def record(message):
    from .windows_app import redact_log
    with _lock:
        if _stream is not None:
            try:
                secrets = tuple(os.environ.get(k, '') for k in ('OPENAI_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'ANTHROPIC_API_KEY'))
                _stream.write(redact_log(str(message), secrets) + '\n')
                _stream.flush()
            except OSError:
                pass


def start():
    global _stream
    try:
        folder = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'YeonE_OCR' / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('session-' + datetime.now().strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}.log')
        _stream = path.open('a', encoding='utf8', buffering=1)
        faulthandler.enable(_stream, all_threads=True)
        from . import __version__
        record(f'START version={__version__} pid={os.getpid()} at={datetime.now().isoformat()}')
    except (OSError, RuntimeError):
        pass


def finish():
    global _stream
    record('GUI_EXIT at=' + datetime.now().isoformat())
    with _lock:
        if _stream is not None:
            faulthandler.disable()
            _stream.close()
            _stream = None
