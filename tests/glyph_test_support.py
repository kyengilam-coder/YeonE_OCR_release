"""Explicit geometry fixtures for orchestration tests with blank page images."""
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch


@contextmanager
def mock_ink_locations():
    """Supply an ink-location fixture while retaining ordinary crop dimensions.

    These tests exercise answer routing/replay, not image detection. Real blank
    images and uniform-location holds are covered without this fixture elsewhere.
    Returned answer windows must still pass the production location check.
    """
    from yeone_ocr import adjudicate
    locate = adjudicate.locate_glyph

    def fixture(*args, **kwargs):
        found = locate(*args, **kwargs)
        return replace(found, how="ink") if found.how == "uniform" else found

    with patch.object(adjudicate, "locate_glyph", side_effect=fixture):
        yield
