"""Private engine failure evidence, kept outside portable project text."""
import json
import os
import tempfile
import time
from pathlib import Path


def save_failure(project_path, page, engine, error):
    if project_path is None:
        return None
    root = Path(project_path).parent / 'engine-diagnostics'
    root.mkdir(parents=True, exist_ok=True)
    evidence = {'schema': 'engine-failure-v1', 'page': page, 'engine': engine,
                'error_type': type(error).__name__, 'message': str(error)[:4000],
                'details': getattr(error, 'diagnostics', {}), 'recorded_at': time.time()}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=f'p{page:04d}-{engine}-', suffix='.json',
                                     dir=root, delete=False) as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
        path = Path(handle.name)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
