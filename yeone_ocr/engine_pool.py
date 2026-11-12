"""Resident local engine workers and engine-level parallelism for page loops.

Without this, every page starts a fresh worker per engine and reloads (and
re-verifies) its models. A pool keeps one RecognitionSession per engine for a
whole run: the worker is reused for up to ``max_requests`` pages and then
restarted, exactly as the existing session protocol bounds it.

Only CPU sessions of the engines whose workers speak the session protocol on
horizontal pages are pooled. Vertical pages and Tesseract keep the one-shot
path, whose post-processing those results depend on. Results are the same
recognitions; only process lifetime changes.
"""
from __future__ import annotations

import os
import threading

from ab_ocr import ocr

SESSION_ENGINES = ("ppocrv6", "rapidocr", "easyocr")
GPU_SESSION_ENGINES = ("surya", "glmocr", "paddle_server")   # always through a CUDA session (FIFO GPU lease)
PARALLEL_ENV = "YEONE_OCR_ENGINE_PARALLEL"
SESSION_ENV = "YEONE_OCR_RESIDENT_WORKERS"
CORES_PER_ENGINE = 4            # each CPU engine already uses several threads (ONNX intra-op ≤ 8, OMP ≤ 8)
MEMORY_HEADROOM = 0.8           # never plan more than this share of the free memory


def engine_parallelism(engines, requested=0, *, resident=(), memory=None, cpu_count=None):
    """How many of ``engines`` may recognize the same page at once.

    ``requested`` > 0 is an explicit choice (capped at the engine count).
    Otherwise YEONE_OCR_ENGINE_PARALLEL, then one engine per CORES_PER_ENGINE
    cores, reduced until the heaviest engines' memory budgets fit in the free
    memory (resident workers already hold theirs).
    """
    from .automatic_ocr import MEMORY_BUDGET_MB
    engines = list(engines)
    if not engines:
        return 1
    gpu = [e for e in engines if e in GPU_SESSION_ENGINES]
    if not requested:
        try:
            requested = int(os.environ.get(PARALLEL_ENV, "0") or 0)
        except ValueError:
            requested = 0
    if requested > 0:
        return max(1, min(requested, len(engines)))
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    # GPU engines need no CPU slot (one GPU lease at a time serializes them on the GPU).
    cpu_engines = len(engines) - len(gpu)
    count = max(1, min(cpu_engines, cores // CORES_PER_ENGINE) + len(gpu)) if cpu_engines else max(1, len(gpu))
    count = min(count, len(engines))
    if memory:
        free = min(memory["available_commit_mb"], memory["available_physical_mb"]) * MEMORY_HEADROOM
        budgets = sorted((MEMORY_BUDGET_MB.get(e, 1024) for e in engines if e not in resident), reverse=True)
        while count > 1 and sum(budgets[:count]) > free:
            count -= 1
    return count


def resident_enabled() -> bool:
    return os.environ.get(SESSION_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


class EnginePool:
    """``recognize`` has the signature of ab_ocr.ocr.recognize_file.

    One session per (engine, languages, root, direction, timeout). A session
    is used by one thread at a time (a lock per session). If a session refuses
    further requests after a cleanup failure, that engine falls back to
    one-shot workers for the rest of the run instead of failing every page.
    """

    def __init__(self, *, max_requests=16, should_stop=None, enabled=None, log=None):
        self.max_requests, self.should_stop, self.log = max_requests, should_stop, log
        self.enabled = resident_enabled() if enabled is None else enabled
        self._sessions: dict[tuple, object] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._one_shot: set[str] = set()
        self._gpu_failed: dict[str, str] = {}
        self._gpu_threads: dict[str, object] = {}      # engine -> single-thread executor
        self._guard = threading.Lock()

    def _on_gpu_thread(self, engine, function, *args, **kwargs):
        """Run on this GPU engine's own thread.

        The PC-wide GPU lease is a Windows mutex: it must be released by the
        thread that took it. run_page uses a fresh thread pool for every page,
        so every start, request and close of a CUDA session goes through one
        thread that lives as long as the pool.
        """
        from concurrent.futures import ThreadPoolExecutor
        with self._guard:
            executor = self._gpu_threads.get(engine)
            if executor is None:
                executor = self._gpu_threads[engine] = ThreadPoolExecutor(1, thread_name_prefix=f"yeone-gpu-{engine}")
        return executor.submit(function, *args, **kwargs).result()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def resident(self) -> set[str]:
        with self._guard:
            return {key[0] for key, session in self._sessions.items() if getattr(session, "worker", None) is not None}

    def _key(self, engine, languages, engine_root, direction, timeout):
        return (engine, languages, str(engine_root), direction, float(timeout))

    def recognize(self, path, *, engine, page_number=1, languages="chi_tra", psm=None, dpi=300, timeout=300.0,
                  should_stop=None, engine_root=None, direction=None, **options):
        if engine in GPU_SESSION_ENGINES:
            return self._recognize_gpu(path, engine=engine, page_number=page_number, languages=languages, psm=psm,
                                       dpi=dpi, timeout=timeout, should_stop=should_stop, engine_root=engine_root)
        if (not self.enabled or engine not in SESSION_ENGINES or direction != "horizontal"
                or engine in self._one_shot or options):
            kwargs = dict(options)
            if direction is not None:
                kwargs["direction"] = direction
            return ocr.recognize_file(path, engine=engine, page_number=page_number, languages=languages, psm=psm,
                                      dpi=dpi, timeout=timeout, should_stop=should_stop, engine_root=engine_root, **kwargs)
        from ab_ocr.local_engines import RecognitionSession
        key = self._key(engine, languages, engine_root, direction, timeout)
        with self._guard:
            session = self._sessions.get(key)
            if session is None:
                session = RecognitionSession(engine, languages, engine_root=engine_root, device="cpu", timeout=timeout,
                                             should_stop=should_stop or self.should_stop,
                                             max_requests=self.max_requests, direction=direction)
                session.__enter__()
                self._sessions[key], self._locks[key] = session, threading.Lock()
            lock = self._locks[key]
        with lock:
            if session.cleanup_failures:
                self._retire(key, engine)
                return self.recognize(path, engine=engine, page_number=page_number, languages=languages, psm=psm,
                                      dpi=dpi, timeout=timeout, should_stop=should_stop, engine_root=engine_root,
                                      direction=direction)
            return session.recognize_file(path, page_number=page_number, dpi=dpi, psm=psm)

    def _recognize_gpu(self, path, *, engine, page_number, languages, psm, dpi, timeout, should_stop, engine_root):
        """A GPU engine whose session cannot start (no CUDA device, GPU queue unavailable)
        is reported once and then refused quickly for the rest of the run."""
        from ab_ocr.local_engines import RecognitionSession
        if engine in self._gpu_failed:
            raise ocr.OCRError(f"{engine} GPU 실행을 이번 실행에서 제외했습니다: {self._gpu_failed[engine]}")
        with self._guard:
            other = next((key[0] for key in self._sessions if key[0] in GPU_SESSION_ENGINES and key[0] != engine), None)
        if other is not None:
            # Its resident session holds the PC-wide GPU queue until closed: waiting would never end.
            raise ocr.OCRError(f"{engine}: 이 실행은 이미 {other} 가 그래픽카드를 쓰고 있어 함께 실행하지 않습니다.")
        key = self._key(engine, languages, engine_root, "horizontal", timeout)
        notices = {"at": None}

        def waiting(event):
            """The PC-wide GPU queue is held by another process: say so every 30 s."""
            elapsed = event.get("elapsed_seconds", 0)
            if self.log and (notices["at"] is None or elapsed - notices["at"] >= 30):
                notices["at"] = elapsed
                self.log(f"  {engine}: GPU 대기 중(앞 순서 {event.get('position', '?')}, {elapsed:.0f}초) — "
                         "다른 프로그램이 그래픽카드 엔진을 쓰고 있습니다")
        with self._guard:
            session = self._sessions.get(key)
            first = session is None
            if first:
                session = RecognitionSession(engine, languages, engine_root=engine_root, device="cuda", timeout=timeout,
                                             should_stop=should_stop or self.should_stop, max_requests=self.max_requests,
                                             on_wait=waiting)
                session.__enter__()
                self._sessions[key], self._locks[key] = session, threading.Lock()
            lock = self._locks[key]
        with lock:
            try:
                return self._on_gpu_thread(engine, session.recognize_file, path, page_number=page_number, dpi=dpi, psm=psm)
            except ocr.OCRCancelled:
                raise
            except (ocr.OCRError, OSError) as error:
                if first:
                    self._gpu_failed[engine] = type(error).__name__
                    if self.log:
                        self.log(f"  {engine}: GPU 실행을 시작하지 못해 이번 실행에서는 CPU 엔진만 씁니다({type(error).__name__}).")
                raise

    def _retire(self, key, engine):
        with self._guard:
            session = self._sessions.pop(key, None)
            self._locks.pop(key, None)
            self._one_shot.add(engine)
        if session is not None:
            try:
                session.__exit__(RuntimeError, None, None)
            except Exception:
                pass
        if self.log:
            self.log(f"  {engine}: 상주 작업자 정리 오류로 이번 실행의 남은 쪽은 쪽마다 새 작업자로 읽습니다.")

    def close(self):
        with self._guard:
            sessions, self._sessions, self._locks = list(self._sessions.items()), {}, {}
        failures = 0
        for key, session in sessions:
            try:
                if key[0] in self._gpu_threads:
                    self._on_gpu_thread(key[0], session.close)       # release the GPU lease on its owning thread
                else:
                    session.close()
            except Exception:
                failures += 1
        with self._guard:
            executors, self._gpu_threads = list(self._gpu_threads.values()), {}
        for executor in executors:
            executor.shutdown(wait=True)
        if failures and self.log:
            self.log(f"  상주 작업자 {failures}개의 임시 파일 정리를 확인하지 못했습니다. 판독 결과는 저장되었습니다.")
