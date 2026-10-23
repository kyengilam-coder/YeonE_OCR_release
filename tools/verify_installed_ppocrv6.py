"""Exercise the installed EXE, bundled interpreter and real models offline.

Uses only the public synthetic Chinese fixture. No installed source imports are
replaced with repository code; recognition is invoked through the frozen CLI.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def distance(a, b):
    a, b = "".join(a.split()), "".join(b.split())
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        following = [i]
        for j, y in enumerate(b, 1):
            following.append(min(following[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = following
    return row[-1], len(a)


def verify(app, fixture, output, automatic=False):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    app, fixture, output = app.resolve(), fixture.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    engine = app / "tools/ppocrv6"
    env = {k: v for k, v in os.environ.items() if k.upper() not in
           {"PYTHONPATH", "PYTHONHOME", "YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT"}}
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", OMP_THREAD_LIMIT="2",
               HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    env["PATH"] = os.environ.get("SystemRoot", "C:\\Windows") + "\\System32"
    runtime = engine / "runtime/python.exe"
    code = """
import sys,json,ctypes,importlib.metadata as m
from pathlib import Path
root=Path(sys.executable).parent.resolve()
assert sys.prefix==sys.base_prefix
assert all(Path(p).resolve().is_relative_to(root) for p in sys.path)
import rapidocr,onnxruntime,cv2,numpy
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.GetModuleHandleW.restype=ctypes.c_void_p
kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint]
crt={}
for name in ('msvcp140.dll','msvcp140_1.dll','vcruntime140.dll','vcruntime140_1.dll'):
    handle=kernel.GetModuleHandleW(name)
    buffer=ctypes.create_unicode_buffer(32768)
    assert handle, name
    assert kernel.GetModuleFileNameW(handle,buffer,len(buffer))
    assert Path(buffer.value).is_relative_to(root), buffer.value
    crt[name]=buffer.value
print(json.dumps({'python':sys.version.split()[0],'paths':sys.path,
                 'rapidocr':m.version('rapidocr'),'onnxruntime':m.version('onnxruntime'),'crt':crt}))
"""
    result = subprocess.run([str(runtime), "-I", "-B", "-c", code], cwd=output, env=env,
                            capture_output=True, text=True, timeout=60, check=True)
    legacy = output / "legacy saved folder"
    legacy.mkdir()
    (legacy / "keep-settings.txt").write_text("existing engine home remains selected", encoding="utf-8")
    settings = legacy / "legacy-settings.json"
    settings.write_text(json.dumps({"local_engine_root": str(legacy)}), encoding="utf-8")
    settings_before = settings.read_bytes()
    report = {"installed_app": str(app), "runtime": json.loads(result.stdout), "automatic": automatic,
              "legacy_engine_root": str(legacy),
              "path": env["PATH"], "cases": [], "network": "worker blocks network; no download", "ok": False}
    pdf = fixture / "chinese_ocr_fixture.pdf"
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != "57863bf04cb406ad06e568974c5f57ec86f21da8fa8ddec885350756998a9db5":
        raise ValueError("The original comparison PDF must not change")

    def recognize(name, source, reference, profile, direction, page=1):
        project = output / (name + ".yeone-compare.json")
        argv = [str(app / "YeonE_OCR.exe"), "--cli", "compare-engines", str(source),
                "--pages", str(page), "--profile", profile, "--direction", direction,
                "--dpi", "200", "--project", str(project), "--engine-root", str(legacy)]
        if not automatic:
            argv.extend(["--engines", "ppocrv6"])
        result = subprocess.run(argv, cwd=output, env=env, capture_output=True, timeout=300)
        (output / (name + ".log")).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Installed OCR failed: {name}, exit {result.returncode}")
        data = json.loads(project.read_text(encoding="utf-8"))
        # ComparisonProject stores page records in an object indexed by number.
        pages = data["pages"]
        selected = pages[str(page)] if isinstance(pages, dict) else next(p for p in pages if p["number"] == page)
        run = selected["runs"][-1]
        if run.get("engine") != "ppocrv6" or "raw" not in run:
            raise ValueError("Installed PP-OCRv6 result missing")
        assert Path(run["request"]["engine_root"]).resolve() == app
        if automatic:
            decision = selected["auto_selections"][-1]
            assert decision["selected_engine"] == "ppocrv6" and decision["applied"]
            assert selected["baseline_engine"] == "ppocrv6"
        errors, count = distance(reference, run["raw"]["text"])
        metadata = run["raw"]["engine_metadata"]
        if profile == "traditional" and direction == "horizontal":
            assert metadata["recognition_policy"] == "ppocrv6-traditional-original-crop-verified-v2"
        record = {"case": name, "errors": errors, "reference_characters": count,
                  "project": str(project), "profile": profile, "direction": direction}
        report["cases"].append(record)
        print(json.dumps(record), flush=True)
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for page, profile, direction in ((1, "simplified", "horizontal"), (2, "traditional", "horizontal"), (3, "traditional", "vertical")):
        ref = (fixture / f"page_{page:02}.reference.txt").read_text(encoding="utf-8")
        recognize(f"original-page-{page}", pdf, ref, profile, direction, page)
    corpus = ["山川風雨，記錄編號：73；比例：2.75%。", "爲為、為爲；羣群、裏裡、臺台、己已巳。", "中文 有空格，原文 需保留。"]
    conditions = [(f, s, "clean") for f in ("simsun.ttc", "msjh.ttc") for s in (28, 44)]
    conditions += [("simsun.ttc", 36, "blur"), ("batang.ttc", 36, "clean")]
    for font_name, size, effect in conditions:
        font = ImageFont.truetype(str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts" / font_name), size)
        missing = font.getmask(chr(0x10ffff), mode="L")
        missing = (missing.size, bytes(missing))
        unsupported = {c for c in "".join(corpus) if (lambda m: (m.size, bytes(m)))(font.getmask(c, mode="L")) == missing}
        lines = ["".join(c for c in line if c not in unsupported) for line in corpus]
        image = Image.new("L", (1600, 850), 255)
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(lines):
            draw.text((70, 90 + i * 170), line, font=font, fill=0)
        if effect == "blur":
            image = image.filter(ImageFilter.GaussianBlur(.6))
        name = f"additional-{font_name}-{size}-{effect}"
        path = output / (name + ".png")
        image.save(path)
        recognize(name, path, "\n".join(lines), "traditional", "horizontal")
    report.update(ok=all(r["errors"] == 0 for r in report["cases"]),
                  total_errors=sum(r["errors"] for r in report["cases"]),
                  legacy_settings_preserved=settings.read_bytes() == settings_before)
    assert report["legacy_settings_preserved"]
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["ok"]:
        raise RuntimeError("Installed OCR differs from the verified fixture")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--automatic", action="store_true")
    args = parser.parse_args()
    verify(args.app, args.fixture, args.output, args.automatic)
