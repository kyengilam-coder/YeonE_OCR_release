"""엔진 관리: GPU 점검 파싱 · 추천 · 설치 위치 · 명령 구성 · 설치용 Python · 실행·취소 · 결과 기록.

실제 설치기·네트워크·GPU는 쓰지 않는다. 설치기 자리에 임시 스크립트를 두고 진짜 프로세스로 돌린다.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_ocr import runtime
from yeone_ocr import engine_manager as em

GTX_1660 = "NVIDIA GeForce GTX 1660, 610.88, 6144, 1437, 7.5\n"


def gpu(memory=6144, driver="610.88"):
    return em.Gpu("NVIDIA GeForce GTX 1660", driver, memory, 0, "7.5")


def manifest(root, engine, **extra):
    folder = root / "tools" / engine
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "installation.json").write_text(json.dumps({"engine": engine, **extra}), encoding="utf-8")
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    """앱 폴더·사용자 폴더를 임시 위치로: 개발 PC에 설치된 엔진이 시험에 섞이지 않게 한다."""
    app, user = tmp_path / "app", tmp_path / "user" / "local-engines"
    app.mkdir()
    monkeypatch.setattr(runtime, "application_root", lambda: app)
    monkeypatch.setattr(runtime, "user_engine_root", lambda: user)
    for name in ("YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(app=app, user=user, tmp=tmp_path)


# ---------------------------------------------------------------- GPU 점검

def test_parse_reads_name_driver_memory_and_compute_capability():
    [found] = em.parse_nvidia_smi(GTX_1660)
    assert found == em.Gpu("NVIDIA GeForce GTX 1660", "610.88", 6144, 1437, "7.5")
    assert found.driver_version == (610, 88)
    assert "VRAM 6.0 GB" in found.describe() and "드라이버 610.88" in found.describe()


def test_parse_keeps_unavailable_cells_empty_and_drops_malformed_lines():
    text = ("NVIDIA RTX A2000, 551.61, [N/A], [Not Supported], 8.6\n"
            "Failed to initialize NVML: Unknown Error\n"
            "\n"
            "Tesla T4, 572.16, 15360 MiB, 0 MiB, 7.5\n")
    first, second = em.parse_nvidia_smi(text)
    assert (first.memory_mb, first.used_mb, first.compute_cap) == (0, 0, "8.6")
    assert (second.name, second.memory_mb) == ("Tesla T4", 15360)


def test_probe_retries_without_compute_cap_for_old_drivers():
    calls = []

    def run(_executable, fields):
        calls.append(fields)
        if "compute_cap" in fields:
            return SimpleNamespace(returncode=2, stdout="", stderr='Field "compute_cap" is not a valid field to query.')
        return SimpleNamespace(returncode=0, stdout="GeForce GTX 1060, 391.35, 6144, 300\n", stderr="")

    result = em.probe_gpus(run, executable="nvidia-smi")
    assert len(calls) == 2 and "compute_cap" not in calls[1]
    assert result["gpus"] == [em.Gpu("GeForce GTX 1060", "391.35", 6144, 300, "")]


def test_probe_without_nvidia_smi_or_with_failures_reports_no_gpu():
    with patch.object(em, "find_nvidia_smi", return_value=None):
        assert em.probe_gpus()["gpus"] == []

    def broken(_executable, _fields):
        raise OSError("access denied")
    result = em.probe_gpus(broken, executable="nvidia-smi")
    assert result["gpus"] == [] and "access denied" in result["message"]
    failing = lambda _e, _f: SimpleNamespace(returncode=9, stdout="", stderr="NVIDIA-SMI has failed\n")
    assert "NVIDIA-SMI has failed" in em.probe_gpus(failing, executable="nvidia-smi")["message"]


# ---------------------------------------------------------------- 추천·차례

def test_gtx_1660_gets_every_option_recommended(home):
    advice = em.recommend([gpu()], em.statuses())
    assert all(item["recommended"] and item["selectable"] for item in advice.values()), advice


def test_without_nvidia_gpu_options_are_blocked_and_cpu_options_are_only_selectable(home):
    advice = em.recommend([], em.statuses())
    assert {key for key, item in advice.items() if item["selectable"]} == {"paddle_server", "surya"}
    assert not any(item["recommended"] for item in advice.values())
    assert advice["glmocr"]["reason"] == "NVIDIA GPU 필요"


def test_old_driver_and_small_memory_are_explained_not_recommended(home):
    old = em.recommend([gpu(driver="391.35")], em.statuses())
    assert not old["glmocr"]["recommended"] and "CUDA 12" in old["glmocr"]["reason"]
    middle = em.recommend([gpu(driver="551.61")], em.statuses())
    assert middle["surya_cuda"]["recommended"] and not middle["paddle_server_gpu"]["recommended"]
    small = em.recommend([gpu(memory=3072)], em.statuses())
    assert small["paddle_server_gpu"]["recommended"] and not small["surya_cuda"]["recommended"]
    assert small["surya_cuda"]["selectable"] and "VRAM" in small["surya_cuda"]["reason"]


def test_status_distinguishes_cpu_install_from_active_gpu_backend(home):
    manifest(home.user, "paddle_server", execution_provider="cpu")
    manifest(home.user, "surya", backend_device="cuda")
    status = em.statuses()
    assert status["paddle_server"]["installed"] and not status["paddle_server_gpu"]["installed"]
    assert status["surya"]["installed"] and status["surya_cuda"]["installed"]
    assert not status["glmocr"]["installed"]
    advice = em.recommend([gpu()], status)
    assert advice["surya_cuda"] == {"selectable": False, "recommended": False, "reason": "설치됨"}
    (home.user / "tools/surya/installation.json").write_text("not json", encoding="utf-8")
    assert not em.statuses()["surya"]["installed"]


def test_plan_adds_missing_prerequisites_in_install_order(home):
    assert em.plan(["glmocr", "surya_cuda", "paddle_server_gpu"], em.statuses()) == [
        "paddle_server", "paddle_server_gpu", "surya", "surya_cuda", "glmocr"]
    manifest(home.user, "surya")
    assert em.plan(["surya_cuda", "surya"], em.statuses()) == ["surya_cuda"]
    with pytest.raises(em.EngineManagerError):
        em.plan(["easyocr"], {})


# ---------------------------------------------------------------- 설치 위치

def test_root_is_selected_folder_then_environment_then_per_user(home, monkeypatch):
    assert em.install_root() == home.user
    monkeypatch.setenv("YEONE_OCR_ENGINE_ROOT", str(home.tmp / "from env"))
    assert em.install_root() == (home.tmp / "from env").resolve()
    assert em.install_root(str(home.tmp / "chosen")) == (home.tmp / "chosen").resolve()


def test_installed_app_folder_is_never_the_install_target(home):
    """설치판의 앱 폴더에는 PP-OCRv6가 있어 '엔진 홈'으로 보이지만, 새 엔진은 사용자 폴더로 간다."""
    manifest(home.app, "ppocrv6", runtime_layout="standalone-python")
    with patch.object(runtime.sys, "frozen", True, create=True):
        assert runtime.default_engine_root() == home.app
        for option in em.OPTIONS:
            assert em.option_root(option) == home.user


def test_gpu_backend_is_added_where_the_engine_already_lives(home):
    manifest(home.app, "paddle_server")                              # 개발 폴더에 이미 설치된 CPU 엔진
    assert em.option_root(em.OPTION_BY_KEY["paddle_server_gpu"]) == home.app
    assert em.option_root(em.OPTION_BY_KEY["glmocr"]) == home.user


# ---------------------------------------------------------------- 명령 구성

def test_commands_use_absolute_targets_and_never_isolated_mode(home):
    root, python = home.user, home.tmp / "python.exe"
    for option in em.OPTIONS:
        command, cwd = em.build_command(option.key, root, python)
        assert command[:6] == [str(python), "-E", "-s", "-X", "utf8", "-u"]
        assert "-I" not in command and "-P" not in command           # install_surya_cuda 가 install_surya 를 import 한다
        assert Path(command[6]).is_file() and Path(command[6]).name == option.script
        assert cwd == root.resolve()
        assert all(Path(value).is_absolute() for value in command[7:] if not value.startswith("--"))
    tools = root.resolve() / "tools"
    assert em.build_command("paddle_server", root, python)[0][7:] == ["--root", str(root.resolve())]
    assert em.build_command("paddle_server_gpu", root, python)[0][7:] == [
        "--root", str(root.resolve()), "--model-cache", str(tools / "paddle_server" / "models"), "--activate"]
    assert em.build_command("surya", root, python)[0][7:] == ["--target", str(tools / "surya")]
    assert em.build_command("surya_cuda", root, python)[0][7:] == ["--target", str(tools / "surya"), "--activate"]
    assert em.build_command("glmocr", root, python)[0][7:] == [
        "--target", str(tools / "glmocr"), "--download-cache", str(root.resolve() / "downloads" / "glmocr")]


def test_command_arguments_are_accepted_by_the_real_installers(home):
    """설치기의 인자 이름이 바뀌면 여기서 드러난다(--help 는 아무것도 내려받지 않는다)."""
    for option in em.OPTIONS:
        command, _cwd = em.build_command(option.key, home.user, sys.executable)
        done = subprocess.run([*command[:7], "--help"], capture_output=True, text=True, encoding="utf-8")
        assert done.returncode == 0, done.stderr
        for flag in (value for value in command[7:] if value.startswith("--")):
            assert flag in done.stdout, (option.key, flag)


def test_missing_installer_script_is_reported(home):
    with pytest.raises(em.EngineManagerError, match="install_surya.py"):
        em.build_command("surya", home.user, sys.executable, scripts=home.tmp / "nowhere")


def test_installer_environment_allows_downloads_and_drops_user_python_settings(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    environment = em.installer_environment()
    assert "PYTHONPATH" not in environment and "HF_HUB_OFFLINE" not in environment
    assert environment["PYTHONUTF8"] == "1"


# ---------------------------------------------------------------- 설치용 Python

def bundled_python(folder, *, ensurepip=True, launcher=True):
    for relative in ("python.exe", "python313.dll", "python313._pth", "Lib/os.py", "Lib/venv/__init__.py",
                     "Lib/site-packages/onnxruntime/__init__.py", "Lib/__pycache__/os.cpython-313.pyc", "DLLs/_ssl.pyd"):
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_bytes(b"synthetic " + relative.encode())
    if ensurepip:
        (folder / "Lib/ensurepip/_bundled").mkdir(parents=True)
        (folder / "Lib/ensurepip/__init__.py").write_text("")
        (folder / "Lib/ensurepip/_bundled/pip-25.0-py3-none-any.whl").write_bytes(b"wheel")
    if launcher:
        (folder / "Lib/venv/scripts/nt").mkdir(parents=True)
        (folder / "Lib/venv/scripts/nt/venvlauncher.exe").write_bytes(b"launcher")
    return folder


def test_base_python_copy_drops_pth_and_engine_packages(tmp_path):
    source, destination = bundled_python(tmp_path / "app/tools/ppocrv6/runtime"), tmp_path / "한글 사용자/local-engines/python313"
    python = em.prepare_base_python(source, destination)
    assert python == destination.resolve() / "python.exe" and python.is_file()
    assert not (destination / "python313._pth").exists()             # ._pth 가 있으면 venv 의 site-packages 가 무시된다
    assert list((destination / "Lib/site-packages").iterdir()) == []
    assert not (destination / "Lib/__pycache__").exists()
    assert (destination / "DLLs/_ssl.pyd").is_file() and (destination / "Lib/venv/scripts/nt/venvlauncher.exe").is_file()
    assert em.base_python_problems(destination) == []
    assert (source / "python313._pth").is_file() and (source / "Lib/site-packages/onnxruntime/__init__.py").is_file()
    # 다시 부르면 이미 준비된 것을 쓴다(엔진 venv 가 가리키는 Python 을 바꾸지 않는다)
    (destination / "DLLs/_ssl.pyd").write_bytes(b"kept")
    assert em.prepare_base_python(source, destination) == python
    assert (destination / "DLLs/_ssl.pyd").read_bytes() == b"kept"
    assert not list(destination.parent.glob("python313.installing-*"))


def test_old_bundle_without_ensurepip_or_launcher_is_refused_with_a_reason(tmp_path):
    source = bundled_python(tmp_path / "runtime", ensurepip=False, launcher=False)
    assert em.base_python_problems(source) == ["Lib/ensurepip/__init__.py", "Lib/ensurepip/_bundled/pip-*.whl",
                                               "Lib/venv/scripts/nt/venvlauncher.exe"]
    with pytest.raises(em.EngineManagerError, match="최신 설치파일"):
        em.prepare_base_python(source, tmp_path / "user/python313")
    assert not (tmp_path / "user/python313").exists()


def test_unknown_existing_python_folder_is_not_overwritten(tmp_path):
    source, destination = bundled_python(tmp_path / "runtime"), tmp_path / "user/python313"
    destination.mkdir(parents=True)
    (destination / "mine.txt").write_text("user file")
    with pytest.raises(em.EngineManagerError, match="온전하지"):
        em.prepare_base_python(source, destination)
    assert (destination / "mine.txt").read_text() == "user file"


def test_base_python_is_current_interpreter_from_source_and_bundled_copy_when_frozen(home):
    assert em.base_python() == Path(sys.executable)
    with patch.object(em.sys, "frozen", True, create=True):
        with pytest.raises(em.EngineManagerError, match="설치판"):
            em.base_python()                                         # 휴대용 실행 파일: 번들 Python 없음
        bundled_python(home.app / "tools/ppocrv6/runtime")
        python = em.base_python()
        assert python == home.user / "python313" / "python.exe" and python.is_file()
        assert (home.user / "python313" / em.BASE_PYTHON_MARKER).is_file()


# ---------------------------------------------------------------- 실행·취소

def test_process_output_is_split_into_lines_and_progress_updates():
    lines = []
    code = ("import sys\n"
            "sys.stdout.write('pulling 10%\\rpulling 55%\\rpulling 100%\\n')\n"
            "sys.stdout.write('windows line\\r\\n한글 줄\\n')\n"
            "sys.stderr.write('warning on stderr\\n')\n"
            "sys.stdout.write('no newline at end')\n"
            "sys.exit(3)\n")
    assert em.run_process([sys.executable, "-X", "utf8", "-c", code], log=lambda line, transient=False: lines.append((line, transient))) == 3
    assert ("pulling 10%", True) in lines and ("pulling 55%", True) in lines and ("pulling 100%", False) in lines
    assert ("windows line", False) in lines and ("한글 줄", False) in lines
    assert ("warning on stderr", False) in lines and ("no newline at end", False) in lines


def test_cancel_stops_the_installer_and_its_children(tmp_path):
    marker = tmp_path / "child-survived.txt"
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('alive')"
    code = ("import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n")
    cancel, lines = threading.Event(), []

    def log(line, transient=False):
        lines.append(line)
        if line == "started":
            cancel.set()

    began = time.monotonic()
    with pytest.raises(em.Cancelled):
        em.run_process([sys.executable, "-c", code], log=log, cancel=cancel)
    assert time.monotonic() - began < 20 and lines == ["started"]
    time.sleep(3.5)
    assert not marker.exists()                                       # pip·ollama 같은 하위 프로세스도 함께 끝난다


def test_process_that_cannot_start_and_timeout_are_reported(tmp_path):
    with pytest.raises(em.EngineManagerError, match="시작하지 못했습니다"):
        em.run_process([str(tmp_path / "no-such-python")], log=lambda *_: None)
    with pytest.raises(em.EngineManagerError, match="제한 시간"):
        em.run_process([sys.executable, "-c", "import time; time.sleep(30)"], log=lambda *_: None, timeout=0.5)


def test_interrupted_glmocr_install_is_cleared_but_other_content_is_kept(tmp_path):
    target = tmp_path / "tools/glmocr"
    assert not em.clear_incomplete_glmocr(target)
    (target / "runtime").mkdir(parents=True)
    (target / "models/blobs").mkdir(parents=True)
    assert em.clear_incomplete_glmocr(target) and not target.exists()
    (target / "runtime").mkdir(parents=True)
    (target / "installation.json").write_text("{}")
    assert not em.clear_incomplete_glmocr(target) and (target / "runtime").is_dir()   # 끝난 설치
    (target / "installation.json").unlink()
    (target / "내 문서.hwpx").write_text("user file")
    with pytest.raises(em.EngineManagerError, match="알 수 없는 파일"):
        em.clear_incomplete_glmocr(target)
    assert (target / "내 문서.hwpx").is_file()


# ---------------------------------------------------------------- 설치 실행(가짜 설치기, 진짜 프로세스)

FAKE_INSTALLER = '''
import argparse, json, pathlib, sys
ENGINE, EXTRA, FAIL = {engine!r}, {extra!r}, {fail!r}
parser = argparse.ArgumentParser()
parser.add_argument("--root"); parser.add_argument("--target"); parser.add_argument("--model-cache")
parser.add_argument("--download-cache"); parser.add_argument("--activate", action="store_true")
args = parser.parse_args()
folder = pathlib.Path(args.target) if args.target else pathlib.Path(args.root) / "tools" / ENGINE
print("installing into", folder, flush=True)
if FAIL:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runtime").mkdir(exist_ok=True)
    sys.exit("download failed: SHA-256 mismatch")
path = folder / "installation.json"
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {{"engine": ENGINE}}
if args.activate:
    data.update(EXTRA)
folder.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data), encoding="utf-8")
print("한글 출력 확인", flush=True)
'''


def fake_scripts(folder, fail=()):
    folder.mkdir(parents=True, exist_ok=True)
    extras = {"paddle_server_gpu": {"execution_provider": "cuda"}, "surya_cuda": {"backend_device": "cuda"}}
    for option in em.OPTIONS:
        (folder / option.script).write_text(FAKE_INSTALLER.format(
            engine=option.engine, extra=extras.get(option.key, {}), fail=option.key in fail), encoding="utf-8")
    return folder


def test_install_runs_selected_options_in_order_and_records_a_log(home):
    scripts, shown, steps = fake_scripts(home.tmp / "scripts"), [], []
    outcome = em.run_install(["surya_cuda", "paddle_server"], scripts=scripts, verify=False, gpus=[gpu()],
                             log=lambda line, transient=False: shown.append(line),
                             progress=lambda index, total, label, phase: steps.append((index, total, phase)))
    assert [(item["key"], item["status"]) for item in outcome["results"]] == [
        ("paddle_server", "ok"), ("surya", "ok"), ("surya_cuda", "ok")]
    assert outcome["roots"] == [home.user]
    status = em.statuses()
    assert status["surya_cuda"]["installed"] and status["paddle_server"]["installed"] and not status["glmocr"]["installed"]
    assert steps == [(0, 3, "install"), (1, 3, "install"), (2, 3, "install"), (3, 3, "done")]
    text = outcome["log"].read_text(encoding="utf-8")
    assert outcome["log"].parent == home.user / "engine-manager"
    assert "한글 출력 확인" in text and "GTX 1660" in text and "[Surya GPU 가속 (CUDA 12.4)] 완료" in text
    assert "한글 출력 확인" in shown
    # 다시 실행하면 이미 설치된 것은 건너뛴다
    again = em.run_install(["surya_cuda", "paddle_server"], scripts=scripts, verify=False)
    assert again["results"] == []


def test_failed_base_install_skips_its_gpu_backend_and_retry_continues(home):
    broken = fake_scripts(home.tmp / "broken", fail=("surya", "glmocr"))
    outcome = em.run_install(["surya_cuda", "glmocr", "paddle_server"], scripts=broken, verify=False)
    states = {item["key"]: item["status"] for item in outcome["results"]}
    assert states == {"paddle_server": "ok", "surya": "failed", "surya_cuda": "skipped", "glmocr": "failed"}
    assert "SHA-256 mismatch" in outcome["log"].read_text(encoding="utf-8")
    [surya] = [item for item in outcome["results"] if item["key"] == "surya"]
    assert "종료 코드 1" in surya["message"] and "SHA-256 mismatch" in surya["message"]   # 설치기의 마지막 줄을 그대로 보여 준다
    assert (home.user / "tools/glmocr/runtime").is_dir()             # 끊긴 설치의 잔재
    fixed = fake_scripts(home.tmp / "fixed")
    retry = em.run_install([key for key, state in states.items() if state != "ok"], scripts=fixed, verify=False)
    assert [(item["key"], item["status"]) for item in retry["results"]] == [
        ("surya", "ok"), ("surya_cuda", "ok"), ("glmocr", "ok")]
    assert not (home.user / "tools/glmocr/runtime").exists()         # 잔재를 지우고 새로 설치했다


def test_cancel_marks_the_running_and_remaining_options(home):
    scripts = fake_scripts(home.tmp / "scripts")
    (scripts / "install_surya.py").write_text("import time\nprint('downloading', flush=True)\ntime.sleep(60)\n")
    cancel = threading.Event()

    def log(line, transient=False):
        if line == "downloading":
            cancel.set()

    outcome = em.run_install(["paddle_server", "surya", "glmocr"], scripts=scripts, verify=False, log=log, cancel=cancel)
    assert [(item["key"], item["status"]) for item in outcome["results"]] == [
        ("paddle_server", "ok"), ("surya", "cancelled"), ("glmocr", "cancelled")]


def test_verification_failure_keeps_the_install_but_flags_it(home):
    scripts = fake_scripts(home.tmp / "scripts")
    with patch.object(em, "verify_option", return_value=(False, "ONNX Runtime이 CUDA로 뜨지 않았습니다")) as verify:
        outcome = em.run_install(["paddle_server"], scripts=scripts)
    assert outcome["results"][0]["status"] == "unverified" and "CUDA" in outcome["results"][0]["message"]
    assert verify.call_args.args[:2] == ("paddle_server", home.user)
    assert em.statuses()["paddle_server"]["installed"]


def test_verification_reports_an_engine_whose_install_record_is_unusable(home):
    manifest(home.user, "paddle_server")                             # 모델 기록이 없는 설치 기록
    passed, message = em.verify_option("paddle_server", home.user, log=lambda *_: None)
    assert not passed and message


def test_missing_base_python_fails_every_option_without_running_anything(home):
    with patch.object(em, "base_python", side_effect=em.EngineManagerError("설치판이 필요합니다")):
        outcome = em.run_install(["surya", "glmocr"], scripts=fake_scripts(home.tmp / "scripts"))
    assert [(item["status"], item["message"]) for item in outcome["results"]] == [("failed", "설치판이 필요합니다")] * 2
    assert not (home.user / "tools").exists()


def test_command_line_check_lists_options_without_installing(home, capsys):
    with patch.object(em, "probe_gpus", return_value={"gpus": [gpu()], "message": gpu().describe()}):
        assert em.main([]) == 0
    output = capsys.readouterr().out
    assert "GTX 1660" in output and "glmocr" in output and "추천" in output
    assert not home.user.exists()
