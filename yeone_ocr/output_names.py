"""Dated document names; same-day saves keep earlier versions."""
from datetime import datetime, timedelta
from pathlib import Path
import re


def next_document_path(directory, pdf, suffix, *, now=None):
    directory = Path(directory)
    stem = Path(pdf).stem
    moment = now or datetime.now().astimezone()
    extensions = {'.hwpx', '.pdf', '.docx', suffix.lower()}
    output_names = re.compile(rf"({re.escape(stem)} - \d{{6}}_\d{{6}})(?:_\d{{2,}})?", re.I)
    occupied = set()
    if directory.is_dir():
        for path in directory.iterdir():
            match = output_names.fullmatch(path.stem)
            if match and path.suffix.lower() in extensions:
                occupied.add(match[1].casefold())
    # A second-resolution name must still preserve a file saved in the same
    # second. A numbered part also occupies the timestamp of its volume.
    while True:
        name = f'{stem} - {moment:%y%m%d_%H%M%S}'
        if name.casefold() not in occupied:
            return directory / (name + suffix)
        moment += timedelta(seconds=1)


def latest_document_path(directory, pdf, suffix):
    directory=Path(directory);stem=Path(pdf).stem
    pattern=re.compile(re.escape(stem)+r' - (?:\d{6}(?:_\d{6})?|\d{8}(?:_\d{2})?)(?: - v[1-9]\d*)?(?:_\d{2})?'+re.escape(suffix)+r'$',re.I)
    files=[p for p in directory.iterdir() if p.is_file() and pattern.fullmatch(p.name)] if directory.is_dir() else []
    if files:return max(files,key=lambda p:(p.stat().st_mtime_ns,p.name))
    legacy=directory/(stem+'_글자층.pdf') if suffix.lower()=='.pdf' else directory/(stem+suffix)
    return legacy if legacy.is_file() else next_document_path(directory,pdf,suffix)
