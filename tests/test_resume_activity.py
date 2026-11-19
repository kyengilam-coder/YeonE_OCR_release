"""Resume activity text and GUI event routing, without a live user window."""
from types import SimpleNamespace
from yeone_ocr import gui_progress as G
from yeone_ocr.gui import App


def test_activity_labels_distinguish_reuse_review_and_new_ocr():
    parts = G.big_view('run', phase='running', stage='read', done=10, total=484,
                       now_pages=[2, 8, 229], page_activities={2: 'checking', 8: 'ai-review', 229: 'ocr'})
    text = ''.join(t for t, _ in parts)
    assert '저장 기록 확인 2쪽' in text
    assert 'AI 판독·미완료 검증 8쪽' in text
    assert '새 쪽 OCR 229쪽' in text
    assert '판독 중' not in text
    assert parts[0][0] == '처리'


def test_activity_routes_to_live_strip_and_clears_when_done():
    app = SimpleNamespace(running=True, _running_cmd='run', STRIP_LABEL=App.STRIP_LABEL)
    App._on_page(app, 8, 'ai-review')
    assert app._page_activity == {8: 'ai-review'}
    assert app._page_live == {8: 'now'}
    App._on_page(app, 8, 'review_pending')
    assert app._page_activity == {}
    assert app._page_live == {8: 'review_pending'}
