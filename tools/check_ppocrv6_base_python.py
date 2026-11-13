"""Check that a PP-OCRv6 bundle's Python can serve as the engine manager's base interpreter.

Copies <bundle>/runtime the way the installed app does (engine_manager.prepare_base_python:
without ._pth and site-packages) into a new temporary folder, creates a venv with pip from
the copy, and checks that the venv is isolated and has ssl for downloads. Nothing is
downloaded and the bundle is not modified. Windows only (the bundle is a Windows runtime).

    python tools/check_ppocrv6_base_python.py <bundle folder>
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

PROBE = ("import json, ssl, sys, sqlite3, venv, ensurepip; "
         "print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
         "'version': sys.version.split()[0], 'ssl': ssl.OPENSSL_VERSION}))")


def run(command, **kwargs):
    done = subprocess.run([str(part) for part in command], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)
    if done.returncode:
        raise RuntimeError(f"{Path(str(command[0])).name} {' '.join(map(str, command[1:3]))} failed "
                           f"({done.returncode}): {(done.stderr or done.stdout).strip()[-800:]}")
    return done.stdout.strip()


def check(bundle: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    from yeone_ocr.engine_manager import base_python_problems, prepare_base_python
    runtime = Path(bundle).resolve() / "runtime"
    if problems := base_python_problems(runtime):
        raise RuntimeError("Bundle runtime cannot create engine environments: " + ", ".join(problems))
    work = Path(tempfile.mkdtemp(prefix="yeone-base-python-"))
    try:
        python = prepare_base_python(runtime, work / "local-engines" / "python313")
        environment = {key: value for key, value in __import__("os").environ.items()
                       if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
        base = json.loads(run([python, "-E", "-s", "-c", PROBE], env=environment))
        venv_dir = work / "local-engines" / "tools" / "probe" / "venv"
        run([python, "-E", "-s", "-m", "venv", venv_dir], env=environment)
        venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        inside = json.loads(run([venv_python, "-c", PROBE], env=environment))
        pip = run([venv_python, "-m", "pip", "--version"], env=environment)
        if Path(inside["prefix"]).resolve() != venv_dir.resolve():
            raise RuntimeError(f"venv is not isolated: sys.prefix={inside['prefix']}")
        if Path(inside["base_prefix"]).resolve() != python.parent.resolve():
            raise RuntimeError(f"venv does not use the copied base Python: {inside['base_prefix']}")
        if (python.parent / "python313._pth").exists():
            raise RuntimeError("Copied base Python still has python313._pth")
        return {"ok": True, "python_version": base["version"], "openssl": base["ssl"],
                "pip": pip.split(" from ")[0], "venv_prefix_isolated": True}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
