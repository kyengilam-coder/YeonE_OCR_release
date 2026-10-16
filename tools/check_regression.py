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
    os.replace(temporary, path)


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
    return {key: counts[key] for key in ("passed", "failed", "error", "skipped", "incomplete")}


def run(output, modules, timeout):
    output.mkdir(parents=True, exist_ok=False)
    child_root = output / "temp"
    child_root.mkdir()
    results = []
    started = time.monotonic()
    summary = {"format": "yeone-offline-regression", "version": 1,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "isolation": "one process per module; pytest collects unittest and pytest functions",
               "network": "blocked in every child", "module_count": len(modules), "modules": results}
    for index, module in enumerate(modules, 1):
        stem = Path(module).stem
        result_path, log_path = output / (stem + ".json"), output / (stem + ".log")
        # A module name can itself be meaningful document text in legacy tests.
        # Keep temporary source paths neutral rather than embedding test names.
        child_temp = child_root / f"{index:03d}-{uuid.uuid4().hex[:10]}"
        child_temp.mkdir()
        environment = os.environ.copy()
        environment.update(TEMP=str(child_temp), TMP=str(child_temp), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                           PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        argv = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                "--_child", module, "--_child-result", str(result_path), "--_child-temp", str(child_temp)]
        creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        module_started = time.monotonic()
        timed_out = False
        with log_path.open("wb") as stream:
            try:
                process = subprocess.run(argv, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                         stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, **creation)
                code = process.returncode
            except subprocess.TimeoutExpired:
                code, timed_out = None, True
        if result_path.exists():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                payload = {}
        else:
            payload = {}
        counts = _counts(payload)
        complete = payload.get("status") == "complete" and code == 0 and not payload.get("collection_errors")
        entry = {"module": module, "exit_code": code, "timed_out": timed_out,
                 "collected": len(payload.get("collected", [])), "counts": counts,
                 "complete": complete, "duration_seconds": round(time.monotonic() - module_started, 3),
                 "log": log_path.name, "result": result_path.name}
        results.append(entry)
        summary["counts"] = {key: sum(item["counts"][key] for item in results) for key in counts}
        summary["collected"] = sum(item["collected"] for item in results)
        summary["modules_complete"] = sum(item["complete"] for item in results)
        summary["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_json(output / "summary.json", summary)
        state = "PASS" if complete and not counts["skipped"] else "CHECK"
        print(f"[{index}/{len(modules)}] {stem}: {state} {counts} exit={code}", flush=True)
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["success"] = (summary["modules_complete"] == len(modules)
                          and not any(summary["counts"][key] for key in ("failed", "error", "skipped", "incomplete")))
    _write_json(output / "summary.json", summary)
    print(json.dumps({"success": summary["success"], "modules": len(modules), "collected": summary["collected"],
                      "counts": summary["counts"], "report": str(output / "summary.json")}), flush=True)
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
    return run(output.expanduser().resolve(), checked, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
