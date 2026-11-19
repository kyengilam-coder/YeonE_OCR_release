"""Choose a non-destructive starting point before a local-first run."""
import re
from pathlib import Path


def recorded_pages(work):
    """Inspect filenames only; never create a work directory or change records."""
    return sorted({int(m.group(1)) for p in (Path(work) / 'readings').glob('p*.json')
                   if p.is_file() and (m := re.fullmatch(r'p(\d+)\.json', p.name)) and int(m.group(1))>0})
