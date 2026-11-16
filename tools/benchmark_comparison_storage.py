"""Offline save benchmark, using identical synthetic books in fresh processes.

Windows working set/private bytes are sampled externally (2 ms requested), so the
worker's JSON encoder holding the GIL cannot hide its peak. tracemalloc is used
in a separate save (excluded from timings) for additional Python allocations.
Example:
  python -B tools/benchmark_comparison_storage.py --baseline work/incremental-storage/comparison_before.py --output work/storage-benchmark
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _core(baseline=None):
    if baseline:
        import yeone_ocr
        spec = importlib.util.spec_from_file_location("yeone_ocr.comparison", baseline)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        setattr(yeone_ocr, "comparison", module)
        spec.loader.exec_module(module)
        return module
    from yeone_ocr import comparison
    return comparison


def _fixture(args):
    import pymupdf as fitz
    from ab_ocr import ocr
    core = _core(args.baseline)
    source = args.output / f"source-{args.pages}.pdf"
    with fitz.open() as doc:
        for _ in range(args.pages):
            doc.new_page(width=400, height=200)
        doc.save(source)
    project = core.ComparisonProject.open(source)
    rng = random.Random(20261115)
    text = "天地玄黃宇宙洪荒日月盈昃辰宿列張\n" * 160
    for number in range(1, args.pages + 1):
        for engine in ("tesseract", "glmocr", "surya"):
            evidence = "".join(chr(rng.randrange(0x4e00, 0x9000)) for _ in range(3800)) * 20
            words = [{"text": text[i:i+8], "bbox": [10, 10, 100, 30], "confidence": 90,
                      "block": 1, "paragraph": 1, "line": i+1} for i in range(0, 1600, 8)]
            raw = ocr.RecognitionResult(text, words, str(source), project.source_sha256,
                number, 400, 200, "synthetic-benchmark", "chi_tra", 6 if engine == "tesseract" else None,
                0, engine=engine).to_dict()
            raw["engine_metadata"] = {"synthetic_repeated_evidence": evidence}
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
                                            "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})
        project.set_text(number, text + "人工校正一", reason="synthetic first edit")
        project.set_text(number, text + "人工校正二", reason="synthetic second edit")
        project.set_reviewed(number)
    target = args.output / f"legacy-{args.pages}.json"
    project.save(target)
    print(json.dumps({"pages": args.pages, "legacy_bytes": target.stat().st_size,
                      "logical_json_bytes": len(project._encoded())}), flush=True)


def _worker(args):
    core = _core(args.baseline if args.backend == "legacy" else None)
    target = args.output / f"{args.backend}-{args.phase}-{args.pages}.json"
    shutil.copyfile(args.output / f"legacy-{args.pages}.json", target)
    project = core.ComparisonProject.load(target)
    if args.phase == "steady" and args.backend == "incremental":
        project.save()  # one-time conversion is measured separately
    gc.collect()
    print(json.dumps({"ready": True}), flush=True)
    sys.stdin.readline()
    times = []
    repeats = 1 if args.phase == "migration" else args.repeats
    for index in range(repeats):
        project.set_text(1, "benchmark saved correction " + str(index))
        started = time.perf_counter()
        project.save()
        times.append(time.perf_counter() - started)
    print(json.dumps({"seconds": times, "median_seconds": statistics.median(times),
                      "file_bytes": target.stat().st_size}), flush=True)
    sys.stdin.readline()  # parent stops the RSS sampler before tracing/verification
    if args.phase == "migration":
        # Repeat conversion from the same old fixture for the allocation run.
        shutil.copyfile(args.output / f"legacy-{args.pages}.json", target)
        project = core.ComparisonProject.load(target)
    project.set_text(1, "allocation measurement")
    gc.collect()
    tracemalloc.start()
    project.save()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    reopened = core.ComparisonProject.load(target)
    verified = (reopened._id == project._id and reopened.source_sha256 == project.source_sha256
                and reopened.page_count == project.page_count and reopened._pages == project._pages)
    if not verified:
        raise AssertionError("Saved text/history did not round-trip")
    print(json.dumps({"python_allocation_peak_bytes": peak, "roundtrip_verified": verified}), flush=True)


class _Memory(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
        (name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
            "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]


def _measure(command):
    worker = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=ROOT)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Memory), ctypes.c_ulong)
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, worker.pid)
    if not handle:
        worker.kill()
        raise ctypes.WinError(ctypes.get_last_error())
    stop = threading.Event()
    samples = []

    def memory():
        value = _Memory()
        value.cb = ctypes.sizeof(value)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.rss, value.private

    def monitor():
        while not stop.is_set():
            samples.append(memory())
            stop.wait(0.002)

    def line():
        value = worker.stdout.readline()
        if not value:
            raise RuntimeError(worker.stderr.read())
        return json.loads(value)

    thread = None
    try:
        assert line()["ready"]
        baseline = memory()
        thread = threading.Thread(target=monitor)
        thread.start()
        worker.stdin.write("save\n")
        worker.stdin.flush()
        result = line()
        stop.set()
        thread.join()
        result.update({"rss_before_bytes": baseline[0], "private_before_bytes": baseline[1],
                       "rss_peak_bytes": max(s[0] for s in samples),
                       "private_peak_bytes": max(s[1] for s in samples), "memory_samples": len(samples)})
        worker.stdin.write("verify\n")
        worker.stdin.flush()
        result.update(line())
        worker.wait(timeout=120)
        if worker.returncode:
            raise RuntimeError(worker.stderr.read())
        return result
    finally:
        stop.set()
        if thread:
            thread.join()
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 300, 600])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--pages", type=int)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=["legacy", "incremental"])
    parser.add_argument("--phase", choices=["steady", "migration"])
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.baseline = args.baseline.resolve()
    if not args.fixture and not args.worker and args.output.exists() and any(args.output.iterdir()):
        parser.error("Use a new, empty output directory so earlier fixtures/results remain intact.")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.fixture:
        return _fixture(args)
    if args.worker:
        return _worker(args)
    if os.name != "nt":
        parser.error("The external memory sampler currently requires Windows.")
    results = {"python": sys.version, "platform": platform.platform(), "cpu": platform.processor(),
               "logical_cpus": os.cpu_count(), "baseline": str(args.baseline),
               "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
               "comparison_sha256": hashlib.sha256((ROOT / "yeone_ocr/comparison.py").read_bytes()).hexdigest(),
               "storage_sha256": hashlib.sha256((ROOT / "yeone_ocr/comparison_storage.py").read_bytes()).hexdigest(),
               "sampling_seconds_requested": 0.002, "fixtures": [], "runs": []}
    base = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
            "--baseline", str(args.baseline), "--output", str(args.output), "--repeats", str(args.repeats)]
    for pages in args.sizes:
        command = base + ["--pages", str(pages)]
        fixture = subprocess.run(command + ["--fixture"], check=True, capture_output=True, text=True, encoding="utf-8")
        results["fixtures"].append(json.loads(fixture.stdout))
        for backend, phase in (("legacy", "steady"), ("incremental", "migration"), ("incremental", "steady")):
            result = _measure(command + ["--worker", "--backend", backend, "--phase", phase])
            result.update(pages=pages, backend=backend, phase=phase)
            results["runs"].append(result)
            (args.output / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
