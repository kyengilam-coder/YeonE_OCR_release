"""Whether two paths name the same file, including on network drives.

``os.path.samefile`` compares (st_dev, st_ino). Some network file systems
(SMB/NAS shares mapped to a drive letter) report st_ino 0 for every file, so
samefile calls any two files on that drive "the same" and the source-overwrite
guards refused to save a project next to its PDF. Without a file number the
size and modification time decide: different files are told apart, while the
same file reached through two paths (Z:\\x and \\\\server\\share\\x) still matches.
"""
from __future__ import annotations

import os
from pathlib import Path


def same_file(first, second) -> bool:
    first, second = Path(first), Path(second)
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        pass
    try:
        a, b = os.stat(first), os.stat(second)
    except OSError:
        return False
    if a.st_ino and b.st_ino:
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns
