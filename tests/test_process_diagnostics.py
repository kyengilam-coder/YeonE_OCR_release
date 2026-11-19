"""Process diagnostics survive GUI closure and redact credentials."""
from unittest.mock import patch
from yeone_ocr import process_diagnostics as d


def test_persistent_lifecycle_and_progress_are_redacted(tmp_path):
    with patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path), 'OPENAI_API_KEY': 'private-test-token'}), \
            patch.object(d.faulthandler, 'enable') as enable, patch.object(d.faulthandler, 'disable'):
        d.start()
        d.record('page=307 key=private-test-token')
        d.finish()
    path = next((tmp_path / 'YeonE_OCR' / 'logs').glob('session-*.log'))
    text = path.read_text('utf8')
    assert 'START version=' in text and 'GUI_EXIT' in text
    assert 'page=307' in text and 'private-test-token' not in text
    enable.assert_called_once()
