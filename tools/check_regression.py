"""Run every test module in a fresh, offline process, including real Tk tests.

Usage: python -B -X utf8 tools/check_regression.py
Reports, captured output, pytest cache, and temporary documents stay under work/.
Tk's cyclic garbage is collected on the main thread after each test.  Test
modules cannot leave Tcl interpreters or patched globals in a following module.
Both unittest cases and pytest functions are collected by pytest in each child.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

# Explicit source review, not a filename/Tk-string heuristic. These modules use
# pure data or temporary PDF/HWPX/image fixtures and mocked readers. They do not
# create windows, access the clipboard/focus, or run an external OCR engine.
# Every newly added or unreviewed module remains exclusive. Review fixtures and
# imported test helpers again before adding a module or changing its resources.
PARALLEL_SAFE_MODULES = frozenset({
    "tests/test_adjudicate_location_hold.py",
    "tests/test_atomic_save_retries.py",
    "tests/test_basic_styles.py",
    "tests/test_block_ops.py",
    "tests/test_bookmark_cursor_map.py",
    "tests/test_chunks.py",
    "tests/test_collate_color_only.py",
    "tests/test_collate_manual_replay.py",
    "tests/test_collate_memo_cleanup.py",
    "tests/test_collate_section_edit.py",
    "tests/test_comparison_ai.py",
    "tests/test_comparison_ai_refinement.py",
    "tests/test_comparison_ai_sdk.py",
    "tests/test_comparison_ai_source_fidelity.py",
    "tests/test_comparison_ai_state.py",
    "tests/test_comparison_bridge.py",
    "tests/test_comparison_fidelity_v2.py",
    "tests/test_comparison_fidelity_v3.py",
    "tests/test_comparison_fidelity_v4.py",
    "tests/test_comparison_incremental.py",
    "tests/test_comparison_incremental_api.py",
    "tests/test_comparison_pdf_punctuation.py",
    "tests/test_comparison_refinement_batch.py",
    "tests/test_comparison_refinement_consensus.py",
    "tests/test_comparison_refinement_guarded.py",
    "tests/test_comparison_refinement_source.py",
    "tests/test_correction_memory.py",
    "tests/test_evaluate_source_exact.py",
    "tests/test_export_source_preservation.py",
    "tests/test_hwpx_golden.py",
    "tests/test_image_direction.py",
    "tests/test_marks.py",
    "tests/test_merge.py",
    "tests/test_normalization.py",
    "tests/test_normalization_report.py",
    "tests/test_pdf_only.py",
    "tests/test_record_preservation.py",
    "tests/test_text_preservation.py",
})

COUNT_KEYS = ("passed", "failed", "error", "skipped", "incomplete")


def _fingerprint(module):
    digest = hashlib.sha256()
    paths = [ROOT / module]
    for package in ("yeone_ocr", "ab_ocr"):
        paths.extend((ROOT / package).rglob("*.py"))
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows scanners can briefly open the previous receipt without share-delete.
    # Keep the complete pending file and retry only that transient sharing failure.
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 19:
                raise
            time.sleep(min(0.01 * 2 ** attempt, 0.1))


def _offline():
    """Block outbound connections and DNS; mocked providers still work normally."""
    import socket
    def blocked(*_args, **_kwargs):
        raise OSError("External network is disabled by the regression runner.")
    class OfflineSocket(socket.socket):
        connect = blocked
        connect_ex = blocked
        sendto = blocked
    if hasattr(socket.socket, "sendmsg"):
        OfflineSocket.sendmsg = blocked
    socket.socket = OfflineSocket
    socket.create_connection = blocked
    socket.getaddrinfo = blocked


def _child(module, result_path, child_temp):
    sys.path.insert(0, str(ROOT))
    os.environ["TEMP"] = str(child_temp)
    os.environ["TMP"] = str(child_temp)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    _offline()
    # Import on the owning thread before tests exercise background GUI jobs.
    # Disable automatic cyclic collection so a worker cannot finalize Tcl objects.
    import yeone_ocr.gui  # noqa: F401
    gc.disable()
    import pytest

    payload = {"module": module, "status": "collecting", "collected": [], "reports": {},
               "collection_errors": [], "network": "blocked", "tk_gc": "main-thread-after-each-test",
               "source_and_test_sha256": _fingerprint(module)}

    class Results:
        def pytest_collection_finish(self, session):
            payload["collected"] = [item.nodeid for item in session.items]
            payload["status"] = "running"
            _write_json(result_path, payload)

        def pytest_collectreport(self, report):
            if report.failed:
                payload["collection_errors"].append(str(report.longrepr))
                _write_json(result_path, payload)

        def pytest_runtest_logreport(self, report):
            record = {"outcome": report.outcome, "duration_seconds": report.duration}
            if report.failed or report.skipped:
                record["detail"] = str(report.longrepr)
            if getattr(report, "wasxfail", None):
                record["xfail"] = report.wasxfail
            payload["reports"].setdefault(report.nodeid, {})[report.when] = record
            _write_json(result_path, payload)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_teardown(self, item, nextitem):
            yield
            gc.collect()

    started = time.monotonic()
    _write_json(result_path, payload)
    # Windows Tcl may retain native stderr handles across interpreter instances.
    # Preserve OS descriptors and capture Python streams, not native file handles.
    code = pytest.main(["-q", "--capture=sys", "-p", "no:cacheprovider", "-W", "error::pytest.PytestUnraisableExceptionWarning",
                        "-W", "error::pytest.PytestUnhandledThreadExceptionWarning",
                        "--basetemp", str(child_temp / "pytest"), module], plugins=[Results()])
    gc.collect()
    payload.update(status="complete", exit_code=int(code), duration_seconds=round(time.monotonic() - started, 3))
    _write_json(result_path, payload)
    return int(code)


def _counts(payload):
    counts = Counter()
    reports = payload.get("reports", {})
    for node in payload.get("collected", []):
        stages = reports.get(node, {})
        if any(stage.get("outcome") == "failed" for stage in stages.values()):
            counts["failed" if stages.get("call", {}).get("outcome") == "failed" else "error"] += 1
        elif any(stage.get("outcome") == "skipped" for stage in stages.values()):
            counts["skipped"] += 1
        elif stages.get("call", {}).get("outcome") == "passed" and stages.get("teardown", {}).get("outcome") == "passed":
            counts["passed"] += 1
        else:
            counts["incomplete"] += 1
    return {key: counts[key] for key in COUNT_KEYS}


def _run_module(output, index, module, timeout):
    """Own one child, log, receipt and temporary directory; never initialize Tk."""
    stem = Path(module).stem
    result_path, log_path = output / (stem + ".json"), output / (stem + ".log")
    # A module name can itself be meaningful document text in legacy tests.
    child_temp = output / "temp" / f"{index:03d}-{uuid.uuid4().hex[:10]}"
    child_temp.mkdir()
    environment = os.environ.copy()
    environment.update(TEMP=str(child_temp), TMP=str(child_temp), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    argv = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
            "--_child", module, "--_child-result", str(result_path), "--_child-temp", str(child_temp)]
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    module_started = time.monotonic()
    timed_out, launch_error = False, None
    with log_path.open("wb") as stream:
        try:
            process = subprocess.run(argv, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, **creation)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
        except OSError as exc:
            code, launch_error = None, str(exc)
            stream.write((launch_error + "\n").encode("utf-8", errors="replace"))
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, OSError):
        payload = {}
    identifiers = payload.get("collected", [])
    complete = (payload.get("module") == module and payload.get("status") == "complete"
                and payload.get("exit_code") == code == 0 and not payload.get("collection_errors")
                and bool(identifiers) and len(identifiers) == len(set(identifiers)))
    return {"module": module, "exit_code": code, "timed_out": timed_out,
            "collected": len(identifiers), "counts": _counts(payload), "complete": complete,
            "duration_seconds": round(time.monotonic() - module_started, 3),
            "log": log_path.name, "result": result_path.name, "launch_error": launch_error}


def _passed(entry):
    return entry["complete"] and not any(entry["counts"][key] for key in COUNT_KEYS if key != "passed")


def _schedule(modules, workers, prioritize):
    """Priority modules are exclusive; then batch reviewed modules, then others."""
    if workers < 1:
        raise ValueError("workers must be positive")
    if len(modules) != len(set(modules)) or len(prioritize) != len(set(prioritize)):
        raise ValueError("Repeated test module")
    if not set(prioritize).issubset(modules):
        raise ValueError("Priority modules must belong to the selected test coverage")
    remaining = [module for module in modules if module not in prioritize]
    batches = [(False, [module]) for module in prioritize]
    if workers > 1:
        parallel = [module for module in remaining if module in PARALLEL_SAFE_MODULES]
        if parallel:
            batches.append((True, parallel))
        remaining = [module for module in remaining if module not in PARALLEL_SAFE_MODULES]
    batches.extend((False, [module]) for module in remaining)
    return batches


def run(output, modules, timeout, *, workers=1, fail_fast=False, prioritize=()):
    batches = _schedule(modules, workers, prioritize)
    planned = [module for _, batch in batches for module in batch]
    output.mkdir(parents=True, exist_ok=False)
    (output / "temp").mkdir()
    results = []
    started = time.monotonic()
    started_modules = set()
    stop = False
    summary = {"format": "yeone-offline-regression", "version": 1,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "isolation": "one process per module; pytest collects unittest and pytest functions",
               "network": "blocked in every child", "module_count": len(modules), "modules": results,
               "workers": workers, "fail_fast": fail_fast, "prioritized_modules": list(prioritize),
               "requested_modules": list(modules), "scheduled_modules": planned,
               "parallel_modules": [module for parallel, batch in batches if parallel for module in batch],
               "success": False}

    def checkpoint():
        summary["counts"] = {key: sum(item["counts"][key] for item in results) for key in COUNT_KEYS}
        summary["collected"] = sum(item["collected"] for item in results)
        summary["modules_complete"] = sum(item["complete"] for item in results)
        summary["modules_started"] = len(started_modules)
        summary["unrun_modules"] = [module for module in planned if module not in started_modules]
        summary["stopped_early"] = stop
        summary["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_json(output / "summary.json", summary)

    def record(entry):
        nonlocal stop
        results.append(entry)
        # Stable output order does not depend on which worker finishes first.
        results.sort(key=lambda item: planned.index(item["module"]))
        passed = _passed(entry)
        if fail_fast and not passed:
            stop = True
        checkpoint()
        state = "PASS" if passed else "CHECK"
        print(f"[{len(results)}/{len(modules)}] {Path(entry['module']).stem}: "
              f"{state} {entry['counts']} exit={entry['exit_code']}", flush=True)

    checkpoint()
    for parallel, batch in batches:
        if stop:
            break
        if not parallel:
            module = batch[0]
            started_modules.add(module)
            record(_run_module(output, planned.index(module) + 1, module, timeout))
            continue
        # Submit at most workers children. On failure, collect all already-running
        # children, but never start the rest. Serial/GUI work waits for this pool.
        pending = iter(batch)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="regression-child") as executor:
            active = {}

            def fill():
                while not stop and len(active) < workers:
                    module = next(pending, None)
                    if module is None:
                        break
                    started_modules.add(module)
                    future = executor.submit(_run_module, output, planned.index(module) + 1, module, timeout)
                    active[future] = module

            fill()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                # Observe every completed result before refilling: a simultaneous
                # failure must not be hidden by an earlier successful future.
                for future in sorted(done, key=lambda item: planned.index(active[item])):
                    active.pop(future)
                    record(future.result())
                fill()
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["success"] = (len(results) == len(modules) and bool(results) and all(_passed(entry) for entry in results))
    checkpoint()
    print(json.dumps({"success": summary["success"], "modules": len(modules), "collected": summary["collected"],
                      "counts": summary["counts"], "unrun": len(summary["unrun_modules"]),
                      "report": str(output / "summary.json")}), flush=True)
    return 0 if summary["success"] else 1


def consolidate(report_directories, target, note=""):
    """Keep all attempts while selecting the latest complete module outcome.

    A retry must collect exactly the original test IDs.  Where the initial child
    recorded a source fingerprint, a changed application/test tree is rejected.
    This produces an audit artifact; the original reports are never rewritten.
    """
    latest, attempts, first_ids, original = {}, {}, {}, None
    for directory in report_directories:
        directory = directory.expanduser().resolve()
        summary_path = directory / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if (summary.get("format") != "yeone-offline-regression" or not summary.get("finished_at")
                or len(summary.get("modules", [])) != summary.get("module_count")):
            raise ValueError("Every attempt report must describe a finished run.")
        if original is None:
            original = summary
        for entry in summary["modules"]:
            module = entry["module"]
            if directory != report_directories[0].expanduser().resolve() and module not in first_ids:
                raise ValueError("A retry cannot change the original coverage modules.")
            name = entry["result"]
            if Path(name).name != name:
                raise ValueError("Invalid child report path.")
            child_path = directory / name
            child = json.loads(child_path.read_text(encoding="utf-8"))
            identifiers = child.get("collected", [])
            if (child.get("module") != module or len(identifiers) != len(set(identifiers))
                    or entry["counts"] != _counts(child) or entry["collected"] != len(identifiers)):
                raise ValueError("Child report and summary counts disagree.")
            if module in first_ids and set(identifiers) != first_ids[module]:
                raise ValueError("A retry collected different test IDs; full coverage cannot be claimed.")
            first_ids.setdefault(module, set(identifiers))
            fingerprint = child.get("source_and_test_sha256")
            prior = attempts.setdefault(module, [])
            if (prior and fingerprint is not None and prior[0]["source_and_test_sha256"] is not None
                    and fingerprint != prior[0]["source_and_test_sha256"]):
                raise ValueError("Application or test source changed between module attempts.")
            record = {**entry, "summary": str(summary_path), "result": str(child_path),
                      "log": str(directory / entry["log"]), "source_and_test_sha256": fingerprint}
            prior.append(record)
            latest[module] = record
    if original is None or len(latest) != original["module_count"]:
        raise ValueError("The original full module coverage is required.")
    identifiers = [identifier for values in first_ids.values() for identifier in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Test IDs overlap between modules.")
    counts = {key: sum(entry["counts"][key] for entry in latest.values())
              for key in ("passed", "failed", "error", "skipped", "incomplete")}
    success = (all(entry["complete"] and entry["exit_code"] == 0 for entry in latest.values())
               and not any(counts[key] for key in ("failed", "error", "skipped", "incomplete")))
    result = {"format": "yeone-offline-regression-consolidated", "version": 1,
              "finished_at": datetime.now(timezone.utc).isoformat(), "success": success,
              "module_count": len(latest), "collected_unique": len(identifiers), "counts": counts,
              "network": "blocked in every child", "retry_note": note,
              "first_attempt_counts": original["counts"],
              "source_fingerprint_note": "Fingerprints are compared when present in both attempts; earlier runner reports may omit them.",
              "modules": [{**latest[module], "attempts": attempts[module]} for module in latest]}
    target = target.expanduser().resolve()
    if target.exists():
        raise ValueError("Consolidated report already exists; choose a new path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, result)
    print(json.dumps({"success": success, "modules": len(latest), "collected_unique": len(identifiers),
                      "counts": counts, "report": str(target)}), flush=True)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New report directory, default work/regression-<unique>.")
    parser.add_argument("--module", action="append", help="Run selected test file(s); otherwise collect every tests/test*.py.")
    parser.add_argument("--timeout", type=float, default=180.0, help="Maximum seconds per module.")
    parser.add_argument("--workers", type=int, default=1,
                        help="Concurrent children for explicitly reviewed non-GUI modules only (default 1).")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Stop starting modules after a failure; record remaining modules as unrun.")
    parser.add_argument("--prioritize", action="append", default=[],
                        help="Run this selected tests/test*.py first, exclusively; repeat for more modules.")
    parser.add_argument("--consolidate", nargs="+", type=Path, help="Finished report directories, full initial run followed by audited retries.")
    parser.add_argument("--summary", type=Path, help="New consolidated summary JSON path.")
    parser.add_argument("--retry-note", default="", help="Explain why a module was retried in the consolidated audit.")
    parser.add_argument("--_child", help=argparse.SUPPRESS)
    parser.add_argument("--_child-result", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_child-temp", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._child:
        return _child(args._child, args._child_result, args._child_temp)
    if args.consolidate:
        if args.summary is None:
            parser.error("--consolidate requires --summary")
        return consolidate(args.consolidate, args.summary, args.retry_note)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.workers < 1:
        parser.error("--workers must be positive")
    output = args.output or ROOT / "work" / ("regression-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    modules = args.module or [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "tests").glob("test*.py"))]
    if not modules:
        parser.error("No tests found")
    # Limit the runner to repository test files, never execute arbitrary paths.
    checked = []
    for module in modules:
        selected = (ROOT / module).resolve()
        if selected.parent != ROOT / "tests" or not selected.is_file() or not selected.name.startswith("test") or selected.suffix != ".py":
            parser.error("--module must name a tests/test*.py file")
        checked.append(selected.relative_to(ROOT).as_posix())
    if len(checked) != len(set(checked)):
        parser.error("Repeated test module")
    prioritized = []
    for module in args.prioritize:
        selected = (ROOT / module).resolve()
        if selected not in {ROOT / name for name in checked}:
            parser.error("--prioritize must name a module in the selected test coverage")
        prioritized.append(selected.relative_to(ROOT).as_posix())
    if len(prioritized) != len(set(prioritized)):
        parser.error("Repeated priority module")
    return run(output.expanduser().resolve(), checked, args.timeout,
               workers=args.workers, fail_fast=args.fail_fast, prioritize=prioritized)


if __name__ == "__main__":
    raise SystemExit(main())
