"""엔진 관리: 이 PC의 GPU를 점검하고, 선택한 로컬 OCR 엔진을 사용자 폴더에 설치한다.

설치 자체는 scripts/install_*.py(고정 버전·해시 검증)가 한다. 이 모듈은 그 설치기를
별도 Python 프로세스로 차례로 실행하고, 출력·실패·취소를 기록하며, 끝난 뒤 설치를 확인한다.
문서·설정·API 키는 읽지 않는다. Tk에 의존하지 않는다(창은 gui_engines.py).

설치용 Python
- 소스 실행: 지금의 Python(sys.executable).
- 설치판(PyInstaller): 실행 파일로는 venv를 만들 수 없으므로, 함께 설치된 PP-OCRv6의
  독립 Python(tools/ppocrv6/runtime)을 사용자 폴더 local-engines/python313 으로 복사해 쓴다.
  앱을 업데이트하거나 제거해도 이미 설치한 엔진의 venv가 가리키는 Python은 남는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

BASE_PYTHON_DIR = "python313"
BASE_PYTHON_MARKER = "yeone-base-python.json"
LOG_DIR = "engine-manager"
# CUDA 12.x 런타임이 요구하는 Windows 드라이버 하한과, CUDA 12.8 툴킷과 함께 배포된 드라이버.
MIN_DRIVER_CUDA12 = (528, 33)
DRIVER_CUDA_12_8 = (570, 65)
_QUERY_FIELDS = ("name", "driver_version", "memory.total", "memory.used", "compute_cap")


class EngineManagerError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 설치 준비·실행 오류."""


class Cancelled(EngineManagerError):
    pass


@dataclass(frozen=True)
class Gpu:
    name: str
    driver: str = ""
    memory_mb: int = 0
    used_mb: int = 0
    compute_cap: str = ""

    @property
    def driver_version(self) -> tuple[int, ...]:
        parts = []
        for part in self.driver.split("."):
            if not part.isdigit():
                break
            parts.append(int(part))
        return tuple(parts)

    def describe(self) -> str:
        parts = [self.name]
        if self.memory_mb:
            memory = f"VRAM {self.memory_mb / 1024:.1f} GB"
            if self.used_mb:
                memory += f"(사용 중 {self.used_mb / 1024:.1f} GB)"
            parts.append(memory)
        if self.driver:
            parts.append("드라이버 " + self.driver)
        if self.compute_cap:
            parts.append("compute " + self.compute_cap)
        return " · ".join(parts)


@dataclass(frozen=True)
class Option:
    key: str
    engine: str                 # ab_ocr.engine_registry 의 엔진 이름(설치 폴더 tools/<engine>)
    label: str
    summary: str
    gpu: bool = False           # NVIDIA GPU가 있어야 쓸 수 있음
    requires: str | None = None  # 먼저 설치되어 있어야 하는 선택지
    download_gb: float = 0.0    # 대략의 내려받기 크기(측정값 아님 — 안내용)
    disk_gb: float = 0.0        # 대략의 설치 뒤 디스크 사용량(안내용)
    min_vram_mb: int = 0        # 권장 최소 VRAM(가중치 크기에 여유를 둔 기준)
    script: str = ""
    notice: str = ""            # 설치 전에 알릴 것(가중치 라이선스 등)


OPTIONS: tuple[Option, ...] = (
    Option("paddle_server", "paddle_server", "PaddleOCR server (CPU)",
           "간체·번체 한자 문서용 서버 크기 모델 · 한글은 지원하지 않음",
           download_gb=0.4, disk_gb=0.8, script="install_paddle_server.py"),
    Option("paddle_server_gpu", "paddle_server", "PaddleOCR server GPU 가속 (CUDA 12.8)",
           "같은 모델을 NVIDIA GPU로 실행 · CPU 설치는 그대로 보존",
           gpu=True, requires="paddle_server", download_gb=2.0, disk_gb=6.0, min_vram_mb=2048,
           script="install_paddle_server_gpu.py"),
    Option("surya", "surya", "Surya OCR 2 (CPU)",
           "한글·한자 혼용 문서의 주력 엔진 · CPU만으로는 느림",
           download_gb=2.5, disk_gb=4.5, script="install_surya.py",
           notice="가중치 라이선스: Modified AI Pubs Open RAIL-M (docs/SURYA_LOCAL.md)"),
    Option("surya_cuda", "surya", "Surya GPU 가속 (CUDA 12.4)",
           "Surya를 NVIDIA GPU로 실행 · 로컬 우선 인식의 자동 선택에 들어감",
           gpu=True, requires="surya", download_gb=0.7, disk_gb=1.5, min_vram_mb=4096,
           script="install_surya_cuda.py"),
    Option("glmocr", "glmocr", "GLM-OCR (GPU)",
           "간체·번체 한자 문서의 주력 엔진 · NVIDIA GPU 전용",
           gpu=True, download_gb=4.0, disk_gb=7.0, min_vram_mb=4096, script="install_glmocr.py"),
)
OPTION_BY_KEY = {option.key: option for option in OPTIONS}


# ---------------------------------------------------------------- GPU 점검

def parse_nvidia_smi(text: str, fields=_QUERY_FIELDS) -> list[Gpu]:
    """`nvidia-smi --query-gpu=<fields> --format=csv,noheader,nounits` 출력 → GPU 목록.

    값이 없는 칸([N/A], Not Supported)은 비워 둔다. 칸 수가 맞지 않는 줄은 버린다.
    """
    def number(value: str) -> int:
        token = value.split()[0] if value.split() else ""
        try:
            return int(float(token))
        except ValueError:
            return 0

    gpus = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(fields) or not cells[0]:
            continue
        row = {field: ("" if cell.startswith("[") or "not supported" in cell.lower() else cell)
               for field, cell in zip(fields, cells)}
        if not row.get("name"):
            continue
        gpus.append(Gpu(name=row["name"], driver=row.get("driver_version", ""),
                        memory_mb=number(row.get("memory.total", "")), used_mb=number(row.get("memory.used", "")),
                        compute_cap=row.get("compute_cap", "")))
    return gpus


def find_nvidia_smi() -> Path | None:
    if found := shutil.which("nvidia-smi"):
        return Path(found)
    candidates = []
    if root := os.environ.get("SystemRoot"):
        candidates.append(Path(root) / "System32" / "nvidia-smi.exe")
    if program_files := os.environ.get("ProgramFiles"):
        candidates.append(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
    return next((path for path in candidates if path.is_file()), None)


def _query(executable, fields) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run([str(executable), "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=20, creationflags=flags)


def probe_gpus(run=_query, executable=None) -> dict:
    """읽기 전용 GPU 점검. {"gpus": [Gpu…], "message": 한 줄 설명}. 예외를 내지 않는다."""
    executable = executable or find_nvidia_smi()
    if executable is None:
        return {"gpus": [], "message": "NVIDIA GPU를 찾지 못했습니다(nvidia-smi 없음). GPU 가속 엔진은 설치할 수 없습니다."}
    # compute_cap 은 오래된 드라이버의 nvidia-smi에 없다 → 그 칸만 빼고 다시 묻는다.
    for fields in (_QUERY_FIELDS, _QUERY_FIELDS[:-1]):
        try:
            done = run(executable, fields)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"gpus": [], "message": f"nvidia-smi를 실행하지 못했습니다: {exc}"}
        if done.returncode == 0:
            gpus = parse_nvidia_smi(done.stdout, fields)
            if gpus:
                return {"gpus": gpus, "message": " / ".join(gpu.describe() for gpu in gpus)}
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return {"gpus": [], "message": "NVIDIA GPU 정보를 읽지 못했습니다" + (f": {detail[-1][:200]}" if detail else ".")}


# ---------------------------------------------------------------- 위치·상태

def install_root(selected_root=None) -> Path:
    """새 엔진을 설치할 엔진 홈. 설정한 폴더 → 환경 변수 → 사용자별 폴더(앱 폴더에는 설치하지 않는다)."""
    from ab_ocr.runtime import user_engine_root
    if selected_root and str(selected_root).strip():
        return Path(selected_root).expanduser().resolve()
    for variable in ("YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT"):
        if configured := os.environ.get(variable, "").strip().strip('"'):
            return Path(configured).expanduser().resolve()
    return user_engine_root()


def option_root(option: Option, selected_root=None) -> Path:
    """이 선택지를 설치할(또는 이미 설치된) 엔진 홈. 이미 있는 설치에는 그 자리에서 GPU 가속을 더한다."""
    from ab_ocr.runtime import engine_root_for
    found = engine_root_for(option.engine, selected_root)
    if (found / "tools" / option.engine / "installation.json").is_file():
        return found
    return install_root(selected_root)


def _manifest(folder: Path) -> dict | None:
    path = folder / "installation.json"
    try:
        if not path.is_file() or path.stat().st_size > 64_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def option_status(option: Option, selected_root=None) -> dict:
    """설치 기록만 본다(모델 해시 검증은 하지 않음 — 그것은 설치 뒤 확인과 실제 인식 때 한다)."""
    root = option_root(option, selected_root)
    manifest = _manifest(root / "tools" / option.engine)
    base = manifest is not None and manifest.get("engine") == option.engine
    if option.key == "paddle_server_gpu":
        installed = base and manifest.get("execution_provider") == "cuda"
    elif option.key == "surya_cuda":
        installed = base and manifest.get("backend_device") == "cuda"
    else:
        installed = base
    return {"key": option.key, "installed": bool(installed), "root": root,
            "folder": root / "tools" / option.engine}


def statuses(selected_root=None) -> dict[str, dict]:
    return {option.key: option_status(option, selected_root) for option in OPTIONS}


def recommend(gpus: list[Gpu], status: dict[str, dict]) -> dict[str, dict]:
    """선택지마다 {"selectable", "recommended", "reason"}.

    NVIDIA GPU가 없으면 GPU 선택지는 고를 수 없고 CPU 선택지는 권하지 않는다(고를 수는 있음).
    드라이버·VRAM 기준에 못 미치면 고를 수는 있되 권하지 않고 까닭을 적는다.
    """
    best = max(gpus, key=lambda gpu: gpu.memory_mb, default=None)
    result = {}
    for option in OPTIONS:
        if status.get(option.key, {}).get("installed"):
            result[option.key] = {"selectable": False, "recommended": False, "reason": "설치됨"}
            continue
        if not option.gpu:
            result[option.key] = {"selectable": True, "recommended": best is not None,
                                  "reason": "" if best is not None else "GPU 없이도 쓸 수 있으나 느림"}
            continue
        if best is None:
            result[option.key] = {"selectable": False, "recommended": False, "reason": "NVIDIA GPU 필요"}
            continue
        problems = []
        driver = best.driver_version
        if driver and driver < MIN_DRIVER_CUDA12:
            problems.append(f"NVIDIA 드라이버 {best.driver}는 CUDA 12를 지원하지 않음 — 드라이버를 먼저 업데이트")
        elif driver and option.key == "paddle_server_gpu" and driver < DRIVER_CUDA_12_8:
            problems.append(f"CUDA 12.8에는 드라이버 {'.'.join(map(str, DRIVER_CUDA_12_8))} 이상 권장(현재 {best.driver})")
        if best.memory_mb and best.memory_mb < option.min_vram_mb:
            problems.append(f"VRAM {best.memory_mb / 1024:.1f} GB — {option.min_vram_mb / 1024:.0f} GB 이상 권장")
        result[option.key] = {"selectable": True, "recommended": not problems, "reason": " · ".join(problems)}
    return result


def plan(keys, status: dict[str, dict]) -> list[str]:
    """고른 선택지에 아직 없는 선행 설치를 더해 설치 차례대로 돌려준다. 이미 설치된 것은 뺀다."""
    wanted = set()
    for key in keys:
        if key not in OPTION_BY_KEY:
            raise EngineManagerError("알 수 없는 엔진 선택지입니다: " + str(key))
        wanted.add(key)
        if requirement := OPTION_BY_KEY[key].requires:
            wanted.add(requirement)
    return [option.key for option in OPTIONS
            if option.key in wanted and not status.get(option.key, {}).get("installed")]


# ---------------------------------------------------------------- 설치용 Python

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundled_runtime() -> Path | None:
    from ab_ocr.runtime import application_root
    runtime = application_root() / "tools" / "ppocrv6" / "runtime"
    return runtime if (runtime / "python.exe").is_file() else None


def base_python_problems(runtime: Path) -> list[str]:
    """이 독립 Python으로 venv(with pip)를 만들 수 있는지. 빠진 것의 목록(없으면 빈 목록)."""
    problems = []
    for relative in ("python.exe", "Lib/venv/__init__.py", "Lib/ensurepip/__init__.py"):
        if not (runtime / relative).is_file():
            problems.append(relative)
    if not list((runtime / "Lib/ensurepip/_bundled").glob("pip-*.whl")):
        problems.append("Lib/ensurepip/_bundled/pip-*.whl")
    launchers = runtime / "Lib/venv/scripts/nt"
    if not any((launchers / name).is_file() for name in ("venvlauncher.exe", "python.exe")):
        problems.append("Lib/venv/scripts/nt/venvlauncher.exe")
    return problems


def prepare_base_python(source: Path, destination: Path, log=None) -> Path:
    """번들 독립 Python을 사용자 폴더로 복사한다(이미 있으면 그대로 쓴다). python.exe 경로를 돌려준다.

    PP-OCRv6 전용 site-packages 와 ._pth(격리 경로 고정 — venv를 무력화함)는 가져오지 않는다.
    임시 폴더에 다 복사한 뒤 이름을 바꾸므로, 중간에 끊겨도 반쯤 된 Python이 쓰이지 않는다.
    """
    log = log or (lambda _line: None)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    python = destination / "python.exe"
    if destination.exists():
        if (destination / BASE_PYTHON_MARKER).is_file() and not base_python_problems(destination):
            return python
        raise EngineManagerError(f"설치용 Python 폴더가 온전하지 않습니다: {destination}\n"
                                 "이 폴더를 지운 뒤 다시 시도하십시오(설치한 엔진은 다른 폴더에 있습니다).")
    if problems := base_python_problems(source):
        raise EngineManagerError("이 설치판의 Python에는 엔진 설치에 필요한 파일이 없습니다(" + ", ".join(problems)
                                 + ").\n연어OCR을 최신 설치파일로 다시 설치한 뒤 시도하십시오.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for stale in destination.parent.glob(BASE_PYTHON_DIR + ".installing-*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale, ignore_errors=True)
    staging = destination.parent / f"{BASE_PYTHON_DIR}.installing-{os.getpid()}"
    log(f"설치용 Python을 준비합니다: {destination}")

    def ignore(directory, names):
        skipped = {name for name in names if name == "__pycache__" or name.lower().endswith("._pth")}
        if Path(directory).resolve() == source / "Lib":
            skipped.add("site-packages")
        return skipped

    try:
        shutil.copytree(source, staging, ignore=ignore)
        (staging / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        (staging / BASE_PYTHON_MARKER).write_text(json.dumps(
            {"schema_version": 1, "purpose": "base interpreter for locally installed OCR engine environments",
             "copied_from": str(source), "created": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=2), encoding="utf-8")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return python


def base_python(log=None) -> Path:
    """설치기를 실행할 Python. 설치판이면 번들 독립 Python을 사용자 폴더에 준비해 돌려준다."""
    from ab_ocr.runtime import user_engine_root
    if not _frozen():
        executable = Path(sys.executable)
        console = executable.with_name("python.exe")
        return console if executable.name.lower() == "pythonw.exe" and console.is_file() else executable
    destination = user_engine_root() / BASE_PYTHON_DIR
    if (destination / BASE_PYTHON_MARKER).is_file() and not base_python_problems(destination):
        return destination / "python.exe"
    source = bundled_runtime()
    if source is None:
        raise EngineManagerError("엔진을 설치하려면 설치판(Setup)으로 설치한 연어OCR이 필요합니다.\n"
                                 "휴대용 실행 파일에는 설치용 Python이 들어 있지 않습니다.")
    return prepare_base_python(source, destination, log)


# ---------------------------------------------------------------- 명령 구성·실행

def build_command(key: str, root: Path, python: Path, scripts: Path | None = None) -> tuple[list[str], Path]:
    """(명령, 작업 폴더). -I 는 쓰지 않는다: -P 가 켜져 설치기끼리의 import(스크립트 폴더)가 끊긴다."""
    from ab_ocr.runtime import resource_root
    option = OPTION_BY_KEY[key]
    root = Path(root).resolve()
    scripts = Path(scripts) if scripts is not None else resource_root() / "scripts"
    script = scripts / option.script
    if not script.is_file():
        raise EngineManagerError("설치 스크립트가 없습니다: " + option.script)
    command = [str(python), "-E", "-s", "-X", "utf8", "-u", str(script)]
    tools = root / "tools"
    if key == "paddle_server":
        command += ["--root", str(root)]
    elif key == "paddle_server_gpu":
        command += ["--root", str(root), "--model-cache", str(tools / "paddle_server" / "models"), "--activate"]
    elif key == "surya":
        command += ["--target", str(tools / "surya")]
    elif key == "surya_cuda":
        command += ["--target", str(tools / "surya"), "--activate"]
    elif key == "glmocr":
        command += ["--target", str(tools / "glmocr"), "--download-cache", str(root / "downloads" / "glmocr")]
    return command, root


def installer_environment() -> dict[str, str]:
    from ab_ocr.runtime import subprocess_environment
    environment = subprocess_environment()
    environment.pop("HF_HUB_OFFLINE", None)                      # 설치는 공개 파일을 내려받는다(인식 때만 오프라인)
    environment.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1")
    return environment


def run_process(command, cwd=None, *, log, cancel=None, environment=None, timeout=None) -> int:
    """명령을 창 없이 실행하고 출력을 한 줄씩 log(line, transient)로 넘긴다. 종료 코드를 돌려준다.

    transient=True 는 \\r 로 덮어쓰는 진행 표시(내려받기 %)다. 취소하면 하위 프로세스까지 끝내고 Cancelled.
    """
    from ab_ocr.runtime import external_process_search, process_tree
    windows = os.name == "nt"
    kwargs = ({"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004} if windows      # CREATE_SUSPENDED: 작업 개체에 넣은 뒤 시작
              else {"start_new_session": True})
    try:
        with external_process_search():
            process = subprocess.Popen([str(part) for part in command], cwd=str(cwd) if cwd else None,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env=environment or installer_environment(), shell=False, **kwargs)
    except OSError as exc:
        raise EngineManagerError(f"설치 프로세스를 시작하지 못했습니다: {exc}") from exc

    def pump():
        pending = b""
        while chunk := process.stdout.read1(65536):
            pending += chunk
            while True:
                positions = [index for index in (pending.find(b"\n"), pending.find(b"\r")) if index >= 0]
                if not positions:
                    break
                index = min(positions)
                line, separator, pending = pending[:index], pending[index:index + 1], pending[index + 1:]
                if separator == b"\r" and pending[:1] == b"\n":
                    separator, pending = b"\n", pending[1:]
                elif separator == b"\r" and not pending:
                    # \r\n 이 두 조각으로 나뉘어 왔을 수 있다 — 다음 조각을 보고 정한다
                    pending = line + b"\r"
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log(text, separator == b"\r")
        if text := pending.decode("utf-8", errors="replace").strip():
            log(text, False)

    from ab_ocr.ocr import OCRError
    reader = threading.Thread(target=pump, name="engine-install-output", daemon=True)
    deadline = None if timeout is None else time.monotonic() + timeout
    cancelled = timed_out = False
    try:
        with process_tree(process, suspended=windows):
            reader.start()
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    break
                time.sleep(0.1)
            if (cancelled or timed_out) and not windows:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        # Windows: 작업 개체가 닫히며 하위 프로세스(pip · ollama 서버)까지 끝난다.
    except OCRError as exc:                                           # 작업 개체를 만들지 못함 — 하위 프로세스를 책임질 수 없으면 실행하지 않는다
        raise EngineManagerError(f"설치 프로세스를 안전하게 관리할 수 없어 중단했습니다: {exc}") from exc
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if reader.ident is not None:
            reader.join(timeout=5)
        process.stdout.close()
    if cancelled:
        raise Cancelled("설치를 취소했습니다.")
    if timed_out:
        raise EngineManagerError("제한 시간 안에 끝나지 않아 중단했습니다.")
    return process.returncode


def clear_incomplete_glmocr(target: Path, log=None) -> bool:
    """끊긴 GLM-OCR 설치의 잔재를 지운다(설치기는 빈 폴더만 받는다). 지웠으면 True.

    설치 기록(installation.json)이 있거나, 설치기가 만들지 않는 것이 들어 있으면 손대지 않는다.
    """
    target = Path(target)
    if not target.is_dir() or target.is_symlink() or (target / "installation.json").exists():
        return False
    entries = list(target.iterdir())
    if not entries:
        return False
    if any(entry.name not in ("runtime", "models", "venv") and not (entry.is_file() and entry.name.startswith("tmp"))
           for entry in entries):
        raise EngineManagerError(f"GLM-OCR 설치 폴더에 알 수 없는 파일이 있어 다시 설치하지 못합니다: {target}\n"
                                 "폴더를 확인해 비운 뒤 다시 시도하십시오.")
    if log:
        log(f"끊긴 이전 설치를 지우고 다시 시작합니다: {target}", False)
    shutil.rmtree(target)
    return True


# ---------------------------------------------------------------- 설치 뒤 확인

_ORT_CUDA_PROBE = ("import sys, onnxruntime as ort\n"
                   "ort.preload_dlls(directory='')\n"
                   "s = ort.InferenceSession(sys.argv[1], providers=['CUDAExecutionProvider'])\n"
                   "print('providers=' + ','.join(s.get_providers()))\n")


def verify_option(key: str, root: Path, *, log, cancel=None) -> tuple[bool, str]:
    """설치 기록·모델 해시를 확인하고, GPU 선택지는 실제로 CUDA로 뜨는지 본다. (통과, 설명)."""
    from ab_ocr.ocr import doctor, OCRCancelled
    option = OPTION_BY_KEY[key]
    root = Path(root)
    try:
        info = doctor(engine=option.engine, engine_root=root, should_stop=cancel.is_set if cancel else None)
    except OCRCancelled as exc:
        raise Cancelled(str(exc)) from exc
    if not info.get("available"):
        return False, info.get("message") or "설치를 확인하지 못했습니다."
    folder = root / "tools" / option.engine
    lines: list[str] = []

    def collect(line, transient=False):
        lines.append(line)
        log(line, transient)

    if key == "paddle_server_gpu":
        model = next((path for path in sorted((folder / "models").glob("*det*.onnx"))), None)
        if model is None:
            return False, "검출 모델을 찾지 못해 CUDA 실행을 확인하지 못했습니다."
        code = run_process([info["executable"], "-I", "-X", "utf8", "-c", _ORT_CUDA_PROBE, str(model)],
                           log=collect, cancel=cancel, timeout=180)
        if code or not any(line.startswith("providers=CUDAExecutionProvider") for line in lines):
            return False, "ONNX Runtime이 CUDA로 뜨지 않았습니다(NVIDIA 드라이버·여유 VRAM 확인)."
        return True, "CUDAExecutionProvider 세션 생성 확인"
    if key == "surya_cuda":
        servers = sorted((folder / "llama.cpp-cuda").rglob("llama-server.exe"))
        if os.name != "nt" or not servers:
            return True, "설치 기록·해시 확인(이 환경에서는 CUDA 장치 확인을 건너뜀)"
        code = run_process([servers[0], "--list-devices"], cwd=servers[0].parent, log=collect, cancel=cancel, timeout=120)
        if code or not any("CUDA" in line for line in lines):
            return False, "llama.cpp가 CUDA 장치를 찾지 못했습니다(NVIDIA 드라이버 확인)."
        return True, "llama.cpp CUDA 장치 확인"
    return True, "설치 기록·모델 해시 확인"


# ---------------------------------------------------------------- 설치 실행

class InstallLog:
    """설치 로그 파일(엔진 홈/engine-manager/install-*.log)과 화면 콜백에 함께 쓴다."""
    def __init__(self, root: Path, sink=None):
        folder = Path(root) / LOG_DIR
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / ("install-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
        self._file = self.path.open("a", encoding="utf-8", buffering=1)
        self._sink = sink
        self._lock = threading.Lock()

    def __call__(self, line: str, transient: bool = False) -> None:
        with self._lock:
            if not transient and not self._file.closed:
                self._file.write(line + "\n")
        if self._sink is not None:
            self._sink(line, transient)

    def close(self) -> None:
        with self._lock:
            self._file.close()


def run_install(keys, *, selected_root=None, log=None, progress=None, cancel=None, python=None,
                scripts=None, verify=True, gpus=None) -> dict:
    """고른 선택지를 차례로 설치하고 확인한다.

    돌려주는 값: {"results": [{"key","label","status","message"}…], "log": 로그 파일, "roots": [엔진 홈…]}
    status: ok(설치·확인) · unverified(설치됨, 확인 실패) · failed · skipped(선행 설치 실패) · cancelled.
    예외 대신 결과로 알린다. 실패한 것만 다시 고르면 이어서 설치한다(설치기는 받은 파일을 다시 쓴다).
    """
    cancel = cancel or threading.Event()
    progress = progress or (lambda *_args: None)
    status = statuses(selected_root)
    order = plan(keys, status)
    results, roots = [], []
    log_root = install_root(selected_root)
    try:
        writer = InstallLog(log_root, log)
    except OSError as exc:
        return {"results": [{"key": key, "label": OPTION_BY_KEY[key].label, "status": "failed",
                             "message": f"설치 폴더에 쓸 수 없습니다: {log_root} ({exc})"} for key in order],
                "log": None, "roots": []}

    def record(key, state, message=""):
        results.append({"key": key, "label": OPTION_BY_KEY[key].label, "status": state, "message": message})
        writer(f"[{OPTION_BY_KEY[key].label}] " + {"ok": "완료", "unverified": "설치됨 · 확인 실패", "failed": "실패",
                                                    "skipped": "건너뜀", "cancelled": "취소"}[state]
               + (f" — {message}" if message else ""))

    try:
        writer(f"엔진 설치 시작 · {datetime.now().isoformat(timespec='seconds')} · 설치 위치 {log_root}")
        if gpus is not None:
            writer("GPU: " + (" / ".join(gpu.describe() for gpu in gpus) or "NVIDIA GPU 없음"))
        if not order:
            writer("설치할 것이 없습니다(이미 설치됨).")
            return {"results": results, "log": writer.path, "roots": roots}
        need = sum(OPTION_BY_KEY[key].disk_gb for key in order)
        try:
            free = shutil.disk_usage(log_root).free / 1024 ** 3
            if free < need:
                writer(f"주의: 디스크 여유 {free:.1f} GB — 고른 엔진에 대략 {need:.1f} GB가 필요합니다.")
        except OSError:
            pass
        try:
            python = Path(python) if python is not None else base_python(writer)
        except EngineManagerError as exc:
            for key in order:
                record(key, "failed", str(exc))
            return {"results": results, "log": writer.path, "roots": roots}
        writer(f"설치용 Python: {python}")
        failed: set[str] = set()
        for index, key in enumerate(order):
            option = OPTION_BY_KEY[key]
            if cancel.is_set():
                record(key, "cancelled")
                continue
            if option.requires in failed:
                failed.add(key)
                record(key, "skipped", f"{OPTION_BY_KEY[option.requires].label} 설치가 끝나지 않음")
                continue
            progress(index, len(order), option.label, "install")
            root = option_root(option, selected_root)
            writer(f"── {index + 1}/{len(order)} {option.label} → {root / 'tools' / option.engine}")
            try:
                root.mkdir(parents=True, exist_ok=True)
                if key == "glmocr":
                    clear_incomplete_glmocr(root / "tools" / "glmocr", writer)
                command, cwd = build_command(key, root, python, scripts)
                last = [""]

                def step_log(line, transient=False):
                    if not transient:
                        last[0] = line
                    writer(line, transient)

                code = run_process(command, cwd, log=step_log, cancel=cancel)
                if code:
                    failed.add(key)
                    record(key, "failed", f"설치기가 오류로 끝났습니다(종료 코드 {code})" + (f": {last[0][:300]}" if last[0] else "."))
                    continue
                if root not in roots:
                    roots.append(root)
                if not verify:
                    record(key, "ok")
                    continue
                progress(index, len(order), option.label, "verify")
                passed, message = verify_option(key, root, log=writer, cancel=cancel)
                record(key, "ok" if passed else "unverified", message)
            except Cancelled:
                failed.add(key)
                record(key, "cancelled")
            except (EngineManagerError, OSError) as exc:
                failed.add(key)
                record(key, "failed", str(exc))
        progress(len(order), len(order), "", "done")
        return {"results": results, "log": writer.path, "roots": roots}
    finally:
        writer.close()


# ---------------------------------------------------------------- 명령줄(점검·설치)

def main(argv=None) -> int:
    """python -m yeone_ocr.engine_manager [--root 폴더] [--install 선택지…]  — 인자가 없으면 점검만 한다."""
    import argparse
    parser = argparse.ArgumentParser(description="로컬 OCR 엔진 점검·설치")
    parser.add_argument("--root", help="엔진 홈(기본: 사용자별 local-engines)")
    parser.add_argument("--install", nargs="+", choices=[*OPTION_BY_KEY, "recommended"], metavar="선택지",
                        help="설치할 것: " + ", ".join(OPTION_BY_KEY) + " 또는 recommended")
    args = parser.parse_args(argv)
    probe = probe_gpus()
    status = statuses(args.root)
    advice = recommend(probe["gpus"], status)
    print("GPU: " + probe["message"])
    print("설치 위치: " + str(install_root(args.root)))
    for option in OPTIONS:
        state = ("설치됨" if status[option.key]["installed"] else
                 "추천" if advice[option.key]["recommended"] else
                 "선택 가능" if advice[option.key]["selectable"] else "설치 불가")
        reason = advice[option.key]["reason"]
        print(f"  {option.key:18} {state:6} {option.label}" + (f" — {reason}" if reason and reason != "설치됨" else ""))
    if not args.install:
        return 0
    keys = ([key for key, item in advice.items() if item["recommended"]] if "recommended" in args.install
            else list(args.install))
    blocked = [key for key in keys if not advice[key]["selectable"] and not status[key]["installed"]]
    if blocked:
        print("설치할 수 없습니다: " + ", ".join(f"{key}({advice[key]['reason']})" for key in blocked))
        return 2
    outcome = run_install(keys, selected_root=args.root, gpus=probe["gpus"],
                          log=lambda line, transient=False: None if transient else print(line, flush=True))
    if outcome["log"]:
        print("로그: " + str(outcome["log"]))
    return 0 if all(item["status"] == "ok" for item in outcome["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
