"""0.6.8 운영 — 호출 상한(--max-calls) · 1차 측정 손잡이(--page-thinking·--page-media-resolution) · 접속 경로 바꾸기 경쟁 ·
책갈피 교정 3차 판독기 캐시의 키 · 창 프로그램 위생(바깥 프로그램 환경·환율 하루 한 번·기록 창 잠금·쪽별 상태 표 작업 스레드).
모델·네트워크·API 키 없이 가짜 클라이언트로 잰다. 실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import contextlib, io, json, os, queue, sys, tempfile, threading, time, types, unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pydantic
from yeone_ocr import reader_gemini as rg
from yeone_ocr.reader_gemini import ReaderError
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread
from yeone_ocr.state import Work

KEY = "AIza" + "k" * 35
AQ_KEY = "AQ." + "q" * 50


class Out(pydantic.BaseModel):
    x: int


class _OK:
    text = '{"x": 1}'; usage_metadata = None


def _resp(text: str):
    return types.SimpleNamespace(text=text, usage_metadata=None)


@contextlib.contextmanager
def stub_client(gen):
    """rg.make_client 를 가짜로. gen(키, vertexai, kw) 가 응답을 돌려주거나 예외를 던진다. calls 에 (키, 경로, kw), made 에 (키, 경로)가 쌓인다."""
    calls, made = [], []
    lock = threading.Lock()

    class Models:
        def __init__(self, key, vx):
            self.key, self.vx = key, vx

        def generate_content(self, **kw):
            with lock:
                calls.append((self.key, self.vx, kw))
            return gen(self.key, self.vx, kw)

    class Client:
        def __init__(self, key, vx):
            self.models = Models(key, vx)

    def mk(key, vx=False, timeout_ms=0):
        made.append((key, vx)); return Client(key, vx)
    with mock.patch.object(rg, "make_client", mk):
        yield calls, made


def _page_json(n: int = 1) -> str:
    col = lambda: [Paragraph(columns=[Column(col_from_right=1, text="天地玄黃宇宙洪荒")])]
    return PageReading(pdf_page=n, regions=[Region(id="b1", kind="본문", bbox=[50, 830, 700, 950], reading_direction="세로(우→좌)", paragraphs=col())]).model_dump_json(by_alias=True)


def _region_json() -> str:
    return RegionReread(columns=[Column(col_from_right=1, text="天地玄黃宇宙洪荒")]).model_dump_json(by_alias=True)


def _vertical_pdf(path: str, n: int) -> None:
    import pymupdf
    doc = pymupdf.open()
    for _ in range(n):
        pg = doc.new_page(width=500, height=800)
        for k in range(3):
            pg.insert_text((450 - k * 30, 60), "天地玄黃宇宙洪荒日月盈昃辰宿列張", fontname="china-t", fontsize=14, rotate=270)
    doc.save(path); doc.close()


TK_SUBS = ("ttk", "filedialog", "messagebox", "simpledialog", "font")


@contextlib.contextmanager
def gui_module():
    """창 모듈. tkinter 가 없으면(검사 서버) 가짜를 잠시 끼우고, 끝나면 이 시험이 넣은 것만 치운다
    (다른 시험 파일의 'tkinter 없음' 결과를 바꾸지 않게)."""
    try:
        import _tkinter  # noqa: F401
        real = True
    except ImportError:
        real = False
    if real:
        from yeone_ocr import gui
        yield gui
        return
    import yeone_ocr
    names = ["tkinter"] + ["tkinter." + s for s in TK_SUBS] + ["yeone_ocr.gui"]
    added = [n for n in names if n not in sys.modules]
    had_attr = "gui" in vars(yeone_ocr)
    if "tkinter" in added:
        def cls(n):
            return type(n, (), {"__init__": lambda self, *a, **k: None})
        def getter(n):
            return cls(n) if n[:1].isupper() else (lambda *a, **k: None)
        tk = types.ModuleType("tkinter"); tk.__getattr__ = getter; tk.TclError = type("TclError", (Exception,), {})
        for sub in TK_SUBS:
            m = types.ModuleType("tkinter." + sub); m.__getattr__ = getter; setattr(tk, sub, m); sys.modules["tkinter." + sub] = m
        sys.modules["tkinter"] = tk
    try:
        from yeone_ocr import gui
        yield gui
    finally:
        for n in added:
            sys.modules.pop(n, None)
        if not had_attr and "yeone_ocr.gui" in added:
            vars(yeone_ocr).pop("gui", None)


def V(v):
    return types.SimpleNamespace(get=lambda: v, set=lambda x: None)


# ---------------- 1. 호출 상한 ----------------
class CallCap(unittest.TestCase):
    """한 명령에서 모든 판독기가 함께 세는 호출 상한. 재시도·실패한 시도도 센다. 닿으면 부르지 않고 fatal 로 멈춘다. cli.main 마다 새로."""
    def tearDown(self):
        rg.set_call_cap(0)

    def _reader(self, model="gemini-3-pro", **kw):
        return rg.GeminiReader(model=model, api_key=KEY, use_cache=False, **kw)

    def test_next_call_over_cap_is_fatal_without_calling(self):
        with stub_client(lambda k, vx, kw: _OK()) as (calls, _):
            r = self._reader()
            rg.set_call_cap(3)
            for _ in range(3):
                self.assertEqual(r._call(None, "p", Out, extra_parts=["p"]).x, 1)
            with self.assertRaises(ReaderError) as cm:
                r._call(None, "p", Out, extra_parts=["p"])
        e = str(cm.exception)
        self.assertTrue(cm.exception.fatal); self.assertIn("모델 호출 상한 3회에 닿아 멈춤", e); self.assertIn("끝난 쪽은 기록에 남음", e)
        self.assertIn("[세부 설정] 의 호출 상한을 늘리거나 0(상한 없음)", e)
        self.assertEqual(len(calls), 3); self.assertEqual(rg.CALL_CAP.used, 3)                 # 넷째는 부르지 않았다

    def test_retries_and_failed_attempts_count(self):
        class S(Exception):
            code = 503
        with stub_client(lambda k, vx, kw: (_ for _ in ()).throw(S("503 UNAVAILABLE"))) as (calls, _), mock.patch.object(rg.time, "sleep", lambda s: None):
            r = self._reader()
            rg.set_call_cap(2)
            with self.assertRaises(ReaderError) as cm:
                r._call(None, "p", Out, extra_parts=["p"])
        self.assertTrue(cm.exception.fatal); self.assertIn("상한 2회", str(cm.exception))
        self.assertEqual(len(calls), 2)                                                          # 5번 재시도 대신 두 번째 실패 뒤에 멈춤

    def test_shared_by_all_readers_and_zero_means_no_cap(self):
        with stub_client(lambda k, vx, kw: _OK()) as (calls, _):
            a, b = self._reader("gemini-3-pro"), self._reader("gemini-3-flash", thinking="low")
            rg.set_call_cap(2)
            a._call(None, "p", Out, extra_parts=["p"]); b._call(None, "p", Out, extra_parts=["p"])
            for r in (a, b):
                with self.assertRaises(ReaderError):
                    r._call(None, "p", Out, extra_parts=["p"])
            self.assertEqual(len(calls), 2)
            rg.set_call_cap(0)
            for _ in range(5):
                a._call(None, "p", Out, extra_parts=["p"])
            self.assertEqual(len(calls), 7)

    def test_counter_is_thread_safe(self):
        rg.set_call_cap(100); ok, bad = [], []
        def go():
            for _ in range(25):
                try:
                    rg.CALL_CAP.take(); ok.append(1)
                except ReaderError as e:
                    bad.append(e.fatal)
        old = sys.getswitchinterval(); sys.setswitchinterval(1e-6)
        try:
            ts = [threading.Thread(target=go) for _ in range(8)]
            for t in ts: t.start()
            for t in ts: t.join(30)
        finally:
            sys.setswitchinterval(old)
        self.assertEqual((len(ok), len(bad), rg.CALL_CAP.used), (100, 100, 100)); self.assertTrue(all(bad))

    def test_main_resets_cap_for_each_command(self):
        """창 프로그램은 한 프로세스에서 명령을 여럿 돌린다 — 앞 명령이 쓴 호출 수가 다음 명령으로 넘어가지 않는다."""
        from yeone_ocr import cli
        rg.set_call_cap(3); rg.CALL_CAP.used = 3
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["read", os.path.join(tempfile.gettempdir(), "없는_책_ops.pdf"), "--max-calls", "7"])
        self.assertEqual(rc, 2); self.assertEqual((rg.CALL_CAP.limit, rg.CALL_CAP.used), (7, 0)); self.assertIn("모델 호출 상한: 이번 명령에서 7회", buf.getvalue())
        rg.CALL_CAP.used = 7
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["build", os.path.join(tempfile.gettempdir(), "없는_책_ops.pdf")])
        self.assertEqual((rg.CALL_CAP.limit, rg.CALL_CAP.used), (0, 0))                          # 상한 없는 명령은 상한 없음
        ap = cli.build_parser()
        for argv in (["read", "x.pdf"], ["run", "x.pdf"], ["adjudicate", "x.pdf"], ["collate", "x.pdf", "--hwpx", "a.hwpx"]):
            self.assertEqual(ap.parse_args(argv).max_calls, 0, argv)
            self.assertEqual(ap.parse_args(argv + ["--max-calls", "40"]).max_calls, 40, argv)

    def test_read_stops_at_cap_keeps_done_pages_and_next_run_continues(self):
        from yeone_ocr import cli
        page = _page_json()
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "b.pdf"); _vertical_pdf(pdf, 3); w = os.path.join(td, "w")
            argv = ["read", pdf, "--work", w, "--model", "gemini-3-pro", "--max-calls", "2", "--no-second-pass", "--second-model", "", "--second-thinking", "full",
                    "--media-resolution", "default", "--no-cache", "--workers", "1", "--workers-start", "1", "--mode", "vertical", "--dpi", "72"]
            buf = io.StringIO()
            with stub_client(lambda k, vx, kw: _resp(page)) as (calls, _), mock.patch.dict(os.environ, {"GEMINI_API_KEY": KEY}), contextlib.redirect_stdout(buf):
                rc = cli.main(argv)
            log = buf.getvalue()
            self.assertEqual(rc, 1, log); self.assertEqual(len(calls), 2)
            self.assertTrue(any(l.startswith("오류: 3쪽 판독 실패 — 모델 호출 상한 2회에 닿아 멈춤") for l in log.splitlines()), log)
            work = Work(pdf, w)
            self.assertIsNotNone(work.load_record(1)); self.assertIsNotNone(work.load_record(2)); self.assertIsNone(work.load_record(3))
            buf = io.StringIO()
            with stub_client(lambda k, vx, kw: _resp(page)) as (calls2, _), mock.patch.dict(os.environ, {"GEMINI_API_KEY": KEY}), contextlib.redirect_stdout(buf):
                rc = cli.main(argv)                                                              # 다시 누름: 상한을 새로 세고 남은 쪽만
            self.assertEqual(rc, 0, buf.getvalue()); self.assertEqual(len(calls2), 1); self.assertIsNotNone(work.load_record(3))


class CliHelp(unittest.TestCase):
    def test_top_help_lists_every_command_and_run_steps(self):
        """맨 위 --help(cli 모듈 설명)가 명령을 빠짐없이 적고, run 의 단계를 실제 차례(3차 판별·합의 의심·혼동 장부 포함)대로 적는다.
        --no-adjudicate 는 3차 판별만이 아니라 합의 판독 의심도 건너뛴다(cmd_adjudicate 를 아예 부르지 않음)."""
        import argparse, re
        from yeone_ocr import cli
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        head = r"^ {0,2}([a-z]+) "                                                               # 3.13 은 설명 글의 공통 들여쓰기를 지운다
        listed = re.findall(head, cli.__doc__, re.M)
        self.assertEqual(sorted(listed), sorted(sub.choices)); self.assertEqual(len(listed), len(set(listed)))
        run = re.search(r"^ {0,2}run .*?(?=" + head + r"|\Z)", cli.__doc__, re.M | re.S).group(0)
        at = [run.index(w) for w in ("prepare", "read", "adjudicate", "build", "review", "ledger", "report")]
        self.assertEqual(at, sorted(at), run); self.assertIn("합의", run); self.assertIn("--no-adjudicate", run)
        h = sub.choices["run"]._option_string_actions["--no-adjudicate"].help
        self.assertIn("3차 글자 판별", h); self.assertIn("합의 판독 의심", h)


# ---------------- 2. 1차 측정 손잡이 ----------------
class PageKnobs(unittest.TestCase):
    """--page-thinking {auto,low,medium,high} · --page-media-resolution {auto,high,ultra_high}: auto 는 지금과 같고, 설정은 1차에만 간다."""
    def tearDown(self):
        rg.set_call_cap(0)
        from yeone_ocr.cli import apply_mode
        apply_mode(Namespace(mode="vertical"))

    def test_thinking_kw_by_generation(self):
        tk = rg.thinking_kw
        self.assertEqual([tk("gemini-3-pro", lv) for lv in ("low", "medium", "high")], [{"thinking_level": "LOW"}, {"thinking_level": "MEDIUM"}, {"thinking_level": "HIGH"}])
        self.assertEqual(tk("gemini-3.1-pro-preview", "HIGH"), {"thinking_level": "HIGH"})
        self.assertEqual([tk("gemini-2.5-pro", lv) for lv in ("low", "medium", "high")], [{"thinking_budget": 512}, {"thinking_budget": 4096}, {"thinking_budget": 32768}])
        self.assertEqual((tk("gemini-3-pro", None), tk("gemini-3-pro", "auto"), tk("gemini-3-pro", "full")), ({}, {}, {}))

    def test_thinking_is_monotone_for_every_family(self):
        """어느 계열에서나 low ≤ medium ≤ high. 어느 수준도 '모델 기본'(설정 없음)으로 두지 않는다 — 2.5 Flash-Lite 는 기본이 생각 끔이라
        high 를 모델 기본으로 두면 low(512)·medium(4096)보다 덜 생각한다(측정에서 high 를 재면 생각 없음을 잰다)."""
        rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        for m in ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite-preview-09-2025", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.5-pro",
                  "gemini-3-pro", "gemini-3-flash", "gemini-3.1-pro-preview"):
            kws = [rg.thinking_kw(m, lv) for lv in rg.THINKING_LEVELS]
            self.assertTrue(all(kws), (m, kws))
            v = [k.get("thinking_budget") or rank[k["thinking_level"]] for k in kws]
            self.assertEqual(v, sorted(set(v)), (m, kws))                                      # 갈수록 더 생각(같은 값도 없음)
        self.assertEqual([rg.thinking_kw("gemini-2.5-flash-lite", lv) for lv in ("low", "medium", "high")],
                         [{"thinking_budget": 512}, {"thinking_budget": 4096}, {"thinking_budget": 24576}])   # Flash·Flash-Lite 범위 안(최대 24576)
        self.assertEqual(rg.thinking_kw("gemini-2.5-pro-preview-06-05", "high"), {"thinking_budget": 32768})   # 2.5 Pro 최대(128~32768)

    @staticmethod
    def _bare(model, t, thinking=None, thinking_budget=None, media_resolution=None):
        r = rg.GeminiReader.__new__(rg.GeminiReader); r.types = t; r.cache_name = None; r._system = "s"; r.model = model
        r.thinking = thinking; r.thinking_budget = thinking_budget; r.media_resolution = media_resolution; r._lock = threading.Lock()
        return r

    def test_config_construction_with_stub_types(self):
        T = types.SimpleNamespace(ThinkingConfig=lambda **k: ("TC", k), GenerateContentConfig=lambda **k: k, AutomaticFunctionCallingConfig=lambda **k: "afc",
                                  MediaResolution=types.SimpleNamespace(MEDIA_RESOLUTION_HIGH="MR_HIGH"))
        cases = [("gemini-3-pro", "high", None, ("TC", {"thinking_level": "HIGH"})),
                 ("gemini-3.1-pro-preview", "medium", None, ("TC", {"thinking_level": "MEDIUM"})),
                 ("gemini-3-flash", "low", None, ("TC", {"thinking_level": "LOW"})),
                 ("gemini-2.5-pro", "low", None, ("TC", {"thinking_budget": 512})),
                 ("gemini-2.5-pro", "medium", None, ("TC", {"thinking_budget": 4096})),
                 ("gemini-2.5-pro", "high", None, ("TC", {"thinking_budget": 32768})),         # 2.5 의 high 는 그 모델의 최대 예산(모델 기본이 아님)
                 ("gemini-2.5-flash-lite", "high", None, ("TC", {"thinking_budget": 24576})),  # Flash-Lite 의 모델 기본은 생각 끔 — high 가 low 보다 덜 생각하지 않게
                 ("gemini-3-pro", None, None, None),                                             # auto = 오늘과 같음
                 ("gemini-3-pro", "low", 777, ("TC", {"thinking_budget": 777}))]                 # 예산을 직접 주면 그것이 먼저
        for model, th, budget, want in cases:
            cfg = self._bare(model, T, th, budget)._config("S")
            self.assertEqual(cfg.get("thinking_config"), want, (model, th, budget))
            self.assertEqual(cfg["response_schema"], "S"); self.assertEqual(cfg["system_instruction"], "s")
        self.assertEqual(self._bare("gemini-3-pro", T, media_resolution="high")._config("S")["media_resolution"], "MR_HIGH")
        self.assertNotIn("media_resolution", self._bare("gemini-3-pro", T, media_resolution="ultra_high")._config("S"))     # 조각(Part)마다 붙인다
        self.assertNotIn("media_resolution", self._bare("gemini-3-pro", T)._config("S"))

    def test_config_with_real_sdk_types(self):
        try:
            from google.genai import types as gt
        except Exception:
            self.skipTest("google-genai 없음")
        cfg = self._bare("gemini-3-pro", gt, "medium")._config(Out)
        self.assertEqual(cfg.thinking_config.thinking_level, gt.ThinkingLevel.MEDIUM)
        self.assertEqual(self._bare("gemini-2.5-flash", gt, "medium")._config(Out).thinking_config.thinking_budget, 4096)
        self.assertEqual(self._bare("gemini-2.5-flash-lite", gt, "high")._config(Out).thinking_config.thinking_budget, 24576)

    def test_constructor_normalizes_and_settings_note(self):
        with stub_client(lambda k, vx, kw: _OK()):
            mk = lambda model, **kw: rg.GeminiReader(model=model, api_key=KEY, use_cache=False, **kw)
            self.assertEqual(mk("gemini-3-pro", thinking="HIGH").thinking, "high")
            self.assertIsNone(mk("gemini-3-pro", thinking="auto").thinking)
            self.assertEqual(mk("gemini-3-pro", thinking="high", media_resolution="ultra_high").settings_note(), "생각 high · 해상도 ultra_high(조각별)")
            self.assertEqual(mk("gemini-2.5-pro", thinking="medium").settings_note(), "생각 medium(예산 4096) · 해상도 기본")
            self.assertEqual(mk("gemini-2.5-pro", thinking="high", media_resolution="high").settings_note(), "생각 high(예산 32768) · 해상도 high")
            self.assertEqual(mk("gemini-3-pro").settings_note(), "생각 모델 기본 · 해상도 기본")
            self.assertEqual(mk("gemini-3-pro", thinking_budget=777).settings_note(), "생각 예산 777 · 해상도 기본")

    def test_rejected_thinking_level_falls_back_to_model_default(self):
        class Bad(Exception):
            code = 400
        seen = []
        def gen(k, vx, kw):
            seen.append(kw["config"].thinking_config)
            if len(seen) == 1:
                raise Bad("400 INVALID_ARGUMENT: thinking_level is not supported by this model")
            return _OK()
        buf = io.StringIO()
        with stub_client(gen), contextlib.redirect_stdout(buf):
            r = rg.GeminiReader(model="gemini-3-pro", api_key=KEY, use_cache=False, thinking="high")
            self.assertEqual(r._call(None, "p", Out, extra_parts=["p"]).x, 1)
        self.assertIsNotNone(seen[0]); self.assertIsNone(seen[1]); self.assertIsNone(r.thinking)
        self.assertIn("생각 설정을 받지 않아 모델 기본값으로 계속합니다", buf.getvalue())

    def test_make_reader_passes_page_knobs(self):
        from yeone_ocr import cli
        made = []
        class Fake:
            def __init__(self, **kw):
                made.append(kw)
        with mock.patch.object(rg, "GeminiReader", Fake), mock.patch.object(cli, "resolve_mode", lambda *a, **k: "vertical"):
            base = dict(reader="gemini", model="gemini-3-pro", thinking_budget=777, no_cache=False)
            cli.make_reader(Namespace(**base, page_thinking="high", page_media_resolution="ultra_high"), None, None)
            cli.make_reader(Namespace(**base, page_thinking="auto", page_media_resolution="auto"), None, None)
            cli.make_reader(Namespace(**base), None, None)                                       # 선택지가 없는 옛 Namespace 도 지금과 같게
        self.assertEqual((made[0]["thinking"], made[0]["media_resolution"], made[0]["thinking_budget"]), ("high", "ultra_high", None))
        self.assertEqual((made[1]["thinking"], made[1]["media_resolution"], made[1]["thinking_budget"], made[1]["model"]), (None, None, 777, "gemini-3-pro"))
        self.assertEqual(made[2], made[1])

    def test_cli_parsing(self):
        from yeone_ocr.cli import build_parser
        ap = build_parser()
        a = ap.parse_args(["read", "x.pdf"])
        self.assertEqual((a.page_thinking, a.page_media_resolution, a.max_calls), ("auto", "auto", 0))
        a = ap.parse_args(["run", "x.pdf", "--page-thinking", "medium", "--page-media-resolution", "ultra_high"])
        self.assertEqual((a.page_thinking, a.page_media_resolution), ("medium", "ultra_high"))
        a = ap.parse_args(["collate", "x.pdf", "--hwpx", "a.hwpx", "--page-thinking", "low", "--page-media-resolution", "high"])
        self.assertEqual((a.page_thinking, a.page_media_resolution), ("low", "high"))
        for bad in (["--page-thinking", "max"], ["--page-media-resolution", "low"], ["--max-calls", "많이"], ["--max-calls", "-5"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ap.parse_args(["read", "x.pdf"] + bad)

    def _read(self, td, extra, n_pages=1):
        from yeone_ocr import cli
        pdf = os.path.join(td, "b.pdf")
        if not os.path.exists(pdf):
            _vertical_pdf(pdf, n_pages)
        page, region = _page_json(), _region_json()
        gen = lambda k, vx, kw: _resp(page if issubclass(kw["config"].response_schema, PageReading) else region)
        argv = ["read", pdf, "--work", os.path.join(td, "w" + str(len(os.listdir(td)))), "--model", "gemini-3-pro", "--second-model", "", "--second-thinking", "full",
                "--media-resolution", "default", "--second-mode", "each", "--chunk-lines", "0", "--no-cache", "--workers", "1", "--mode", "vertical", "--dpi", "72"] + extra
        buf = io.StringIO()
        with stub_client(gen) as (calls, _), mock.patch.dict(os.environ, {"GEMINI_API_KEY": KEY}), contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        self.assertEqual(rc, 0, buf.getvalue())
        return calls, buf.getvalue()

    def test_page_settings_reach_pass1_only_and_header_describes_run(self):
        from google.genai import types as gt
        with tempfile.TemporaryDirectory() as td:
            calls, log = self._read(td, ["--page-thinking", "high", "--page-media-resolution", "ultra_high"])
            p1 = [c for c in calls if issubclass(c[2]["config"].response_schema, PageReading)]; p2 = [c for c in calls if not issubclass(c[2]["config"].response_schema, PageReading)]
            self.assertEqual(len(p1), 1); self.assertTrue(p2)
            self.assertEqual(p1[0][2]["config"].thinking_config.thinking_level, gt.ThinkingLevel.HIGH)
            self.assertEqual(p1[0][2]["contents"][0].media_resolution.level, gt.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH)   # 쪽 이미지 조각에
            for c in p2:                                                                         # 2차(전사)로 새지 않는다
                self.assertIsNone(c[2]["config"].thinking_config); self.assertIsNone(c[2]["contents"][0].media_resolution)
            head = next(l for l in log.splitlines() if l.startswith("read: 판독기"))
            self.assertIn("gemini-3-pro (1차 생각 high · 해상도 ultra_high(조각별)) · 2차 gemini-3-pro", head)
            calls, log = self._read(td, [])                                                      # auto: 오늘과 같음(설정 없음, 판독기 하나)
            for c in calls:
                self.assertIsNone(c[2]["config"].thinking_config); self.assertIsNone(c[2]["config"].media_resolution); self.assertIsNone(c[2]["contents"][0].media_resolution)
            head = next(l for l in log.splitlines() if l.startswith("read: 판독기"))
            self.assertIn("(1차 생각 모델 기본 · 해상도 기본)", head); self.assertNotIn("· 2차 gemini", head)


# ---------------- 3. 접속 경로 바꾸기 경쟁 ----------------
class RouteSwitch(unittest.TestCase):
    """AQ. 키가 AI Studio 에서 403 이면 Vertex 익스프레스로 한 번 바꾼다. 두 스레드가 함께 403 을 받아도 한 번만, 늦은 스레드는 바뀐 경로로 다시."""
    def tearDown(self):
        rg.set_call_cap(0)

    def test_two_threads_seeing_403_switch_once(self):
        class Denied(Exception):
            code = 403
        bar = threading.Barrier(2, timeout=10)
        def gen(k, vx, kw):
            if not vx:
                bar.wait()                                                                       # 두 스레드가 함께 AI Studio 에서 거절을 받는다
                raise Denied("403 PERMISSION_DENIED")
            return _OK()
        buf = io.StringIO(); res = [None, None]
        with stub_client(gen) as (calls, made), contextlib.redirect_stdout(buf):
            r = rg.GeminiReader(model="gemini-3-pro", api_key=AQ_KEY, use_cache=False)
            def go(i):
                try:
                    res[i] = r._call(None, "p", Out, extra_parts=["p"]).x
                except Exception as e:
                    res[i] = e
            ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
            for t in ts: t.start()
            for t in ts: t.join(30)
        self.assertEqual(res, [1, 1], buf.getvalue())
        self.assertTrue(r.vertexai); self.assertTrue(r._switched)
        self.assertEqual([vx for _, vx in made], [False, True])                                  # 처음 하나 + 바꿀 때 하나(두 번 뒤집지 않음)
        self.assertEqual(buf.getvalue().count("접속 경로로 바꿔"), 1)
        self.assertEqual(sorted(vx for _, vx, _ in calls), [False, False, True, True])

    def test_switched_route_also_denied_stops_with_both_messages(self):
        class Denied(Exception):
            code = 403
        with stub_client(lambda k, vx, kw: (_ for _ in ()).throw(Denied("403 PERMISSION_DENIED"))) as (calls, _), contextlib.redirect_stdout(io.StringIO()):
            r = rg.GeminiReader(model="gemini-3-pro", api_key=AQ_KEY, use_cache=False)
            with self.assertRaises(ReaderError) as cm:
                r._call(None, "p", Out, extra_parts=["p"])
        self.assertTrue(cm.exception.fatal); self.assertIn("AI Studio 경로:", str(cm.exception)); self.assertIn("Vertex 경로:", str(cm.exception))
        self.assertEqual([vx for _, vx, _ in calls], [False, True])

    def test_switch_route_rechecks_state_from_call_start(self):
        with stub_client(lambda k, vx, kw: _OK()) as (_, made), contextlib.redirect_stdout(io.StringIO()):
            r = rg.GeminiReader(model="gemini-3-pro", api_key=AQ_KEY, use_cache=False)
            self.assertTrue(r._switch_route(False, 0)); self.assertTrue(r.vertexai)
            self.assertTrue(r._switch_route(False, 0)); self.assertTrue(r.vertexai)                # 늦게 온 같은 경로의 거절: 다시 뒤집지 않고 새 경로로
            self.assertFalse(r._switch_route(True, 0))                                           # 바꾼 경로에서도 거절이면 끝
            self.assertTrue(r._switch_route(True, 5))                                            # 그사이 키가 바뀌었으면 새 키로 다시
        self.assertEqual([vx for _, vx in made], [False, True])


# ---------------- 4. 책갈피 교정 3차 판독기 캐시 ----------------
class GlyphReaderCache(unittest.TestCase):
    """창에서 API 키를 바꾸면 3차 판독기를 새로 만든다(옛 키로 계속 부르던 문제). 캐시 키는 (모델, 설정, 키 목록 sha1)."""
    def test_cache_follows_api_key(self):
        from yeone_ocr.collate import run as crun
        made = []
        class Fake:
            def __init__(self, model=None, **kw):
                if not rg.split_keys(os.environ.get("GEMINI_API_KEY")):
                    raise ReaderError("GEMINI_API_KEY 가 없습니다.")
                self.model = model; self.key = os.environ.get("GEMINI_API_KEY"); self.kw = kw; made.append(self)
        args = Namespace(model="gemini-3-pro", adjudicate_thinking="low", reader="gemini", no_glyph_check=False)
        a, b = "AIza" + "a" * 35, "AIza" + "b" * 35
        crun._READER_CACHE.clear()
        try:
            with mock.patch.object(rg, "GeminiReader", Fake), mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), contextlib.redirect_stdout(io.StringIO()):
                os.environ.pop("GOOGLE_API_KEY", None)
                self.assertIsNone(crun.glyph_reader(args))                                       # 키가 없을 때 만든 None 이
                os.environ["GEMINI_API_KEY"] = a
                r1 = crun.glyph_reader(args); self.assertIsNotNone(r1)                           # 키를 넣은 뒤까지 남지 않는다
                self.assertIs(crun.glyph_reader(args), r1)
                os.environ["GEMINI_API_KEY"] = f" {a} "; self.assertIs(crun.glyph_reader(args), r1)   # 같은 키(공백만 다름)
                os.environ["GEMINI_API_KEY"] = b
                r2 = crun.glyph_reader(args); self.assertIsNot(r2, r1); self.assertEqual(r2.key, b)
                os.environ["GEMINI_API_KEY"] = f"{a}, {b}"
                r3 = crun.glyph_reader(args); self.assertNotIn(r3, (r1, r2))
                os.environ["GEMINI_API_KEY"] = a
                self.assertIs(crun.glyph_reader(args), r1)
                r4 = crun.glyph_reader(Namespace(**dict(vars(args), adjudicate_thinking="full"))); self.assertNotIn(r4, (r1, r2, r3)); self.assertIsNone(r4.kw["thinking"])
                r5 = crun.glyph_reader(Namespace(**dict(vars(args), model="gemini-3-flash"))); self.assertEqual(r5.model, "gemini-3-flash")
            self.assertEqual(len(made), 5)
            self.assertFalse(any(a in repr(k) or b in repr(k) for k in crun._READER_CACHE))       # 키 자체는 캐시 키에 들고 있지 않다
        finally:
            crun._READER_CACHE.clear()


# ---------------- 5. 창 프로그램 위생 ----------------
class FxFetch(unittest.TestCase):
    def test_fetch_reads_at_most_1mb(self):
        from yeone_ocr import estimate as est
        sizes = []
        bodies = {est.FX_URLS[0]: b'{"rates": {"KRW": 1300}' + b" " * (2 << 20) + b"}", est.FX_URLS[1]: json.dumps({"rates": {"KRW": 1391.2}}).encode()}
        class Resp:
            def __init__(self, body):
                self.body = body
            def read(self, n=-1):
                sizes.append(n); return self.body if n is None or n < 0 else self.body[:n]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with mock.patch.object(est.urllib.request, "urlopen", lambda url, timeout=0: Resp(bodies[url])):
            self.assertEqual(est.fetch_fx_krw(), (1391.2, "오늘 자동 조회"))                      # 첫 응답(2MB)은 버리고 다음 곳
            self.assertEqual(sizes, [est.FX_MAX_BYTES + 1] * 2); self.assertEqual(est.FX_MAX_BYTES, 1 << 20)
            bodies[est.FX_URLS[1]] = json.dumps({"rates": {"KRW": 0}}).encode()
            self.assertIsNone(est.fetch_fx_krw())


class GuiOps(unittest.TestCase):
    def test_child_env_pip_and_open_path_drop_api_keys(self):
        with gui_module() as gui:
            seen = []
            class P:
                def __init__(self, cmd, **kw):
                    seen.append((cmd, kw)); self.stdout = iter(["Collecting pydantic\n"])
                def wait(self):
                    return 0
            env = {"GEMINI_API_KEY": KEY, "GOOGLE_API_KEY": KEY, "YEONE_OPS_KEEP": "1"}
            with mock.patch.dict(os.environ, env):
                ce = gui.child_env()
                self.assertNotIn("GEMINI_API_KEY", ce); self.assertNotIn("GOOGLE_API_KEY", ce); self.assertEqual(ce["YEONE_OPS_KEEP"], "1")
                self.assertEqual(os.environ["GEMINI_API_KEY"], KEY)                              # 이 프로세스(판독)의 키는 그대로
                app = types.SimpleNamespace(running=False, q=queue.Queue(), _set_running=lambda *a, **k: None, v_status=V(""), nb=types.SimpleNamespace(select=lambda i: None))
                with mock.patch.object(gui.subprocess, "Popen", P), mock.patch.object(gui, "missing_packages", lambda: []):
                    gui.App.install_packages(app)
                    while True:
                        item = app.q.get(timeout=20)
                        if item[0] == "done":
                            break
                self.assertEqual(item[1], 0)
                cmd, kw = seen[-1]
                self.assertIn("pip", cmd); self.assertNotIn("GEMINI_API_KEY", kw["env"]); self.assertNotIn("GOOGLE_API_KEY", kw["env"]); self.assertEqual(kw["env"]["YEONE_OPS_KEEP"], "1")
                for plat, opener in (("linux", "xdg-open"), ("darwin", "open")):
                    with mock.patch.object(gui.subprocess, "Popen", P), mock.patch.object(gui.sys, "platform", plat):
                        gui.open_path(Path("/tmp/결과.html"))
                    cmd, kw = seen[-1]
                    self.assertEqual(cmd, [opener, str(Path("/tmp/결과.html"))]); self.assertNotIn("GEMINI_API_KEY", kw["env"]); self.assertNotIn("GOOGLE_API_KEY", kw["env"])

    def test_fx_fetched_at_most_once_a_day(self):
        with gui_module() as gui:
            f = gui.fx_from_config
            self.assertEqual(f({}, "2026-10-11"), (None, True))
            self.assertEqual(f({"fx_auto": 1390.0, "fx_auto_date": "2026-10-11"}, "2026-10-11"), ((1390.0, "오늘 자동 조회"), False))
            self.assertEqual(f({"fx_auto": 1390.0, "fx_auto_date": "2026-10-10"}, "2026-10-11"), ((1390.0, "2026-10-10 자동 조회"), True))   # 지난날 값은 쓰되 새로 조회
            self.assertEqual(f({"fx_auto": "엉뚱", "fx_auto_date": "2026-10-11"}, "2026-10-11"), (None, True))
            self.assertEqual(f({"fx_auto": 0, "fx_auto_date": "2026-10-11"}, "2026-10-11"), (None, True))
            app = types.SimpleNamespace(q=queue.Queue(), cfg={"workers": 8}, _fx=None)
            with mock.patch("yeone_ocr.estimate.fetch_fx_krw", lambda: (1402.5, "오늘 자동 조회")):
                gui.App._fetch_fx(app)                                                           # 작업 스레드: 받은 값은 큐로만
            item = app.q.get_nowait()
            self.assertEqual(item, ("fx", 1402.5, time.strftime("%Y-%m-%d"))); self.assertIsNone(app._fx); self.assertNotIn("fx_auto", app.cfg)
            with mock.patch.object(gui, "save_config") as sc:
                gui.App._fx_fetched(app, item[1], item[2])                                       # 창 스레드: 값을 쓰고 설정에 남긴다
            self.assertEqual(app._fx, (1402.5, "오늘 자동 조회")); sc.assert_called_once_with(app.cfg)
            self.assertEqual((app.cfg["fx_auto"], app.cfg["fx_auto_date"]), (1402.5, item[2]))
            self.assertFalse(gui.fx_from_config(app.cfg, item[2])[1])                           # 같은 날 다시 켜면 묻지 않는다
            with mock.patch("yeone_ocr.estimate.fetch_fx_krw", lambda: None):
                gui.App._fetch_fx(app)
            self.assertTrue(app.q.empty())                                                       # 실패하면 있던 값 그대로

    def test_queue_writer_is_thread_safe(self):
        with gui_module() as gui:
            w = gui.QueueWriter(queue.Queue())
            def go(i):
                for j in range(300):
                    w.write(f"주의: 스레드 {i} 줄 {j}\n" if j % 2 else f"  진행 {i}-{j}\n")
            old = sys.getswitchinterval(); sys.setswitchinterval(1e-6)
            try:
                ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
                for t in ts: t.start()
                for t in ts: t.join(30)
            finally:
                sys.setswitchinterval(old)
            self.assertEqual(len(w.problems), 8 * 150); self.assertEqual(len(set(w.problems)), 8 * 150)
            n = 0
            while not w.q.empty():
                w.q.get_nowait(); n += 1
            self.assertEqual(n, 8 * 300)

    def test_queue_writer_keeps_whole_lines_per_thread(self):
        """print 는 글과 줄바꿈을 따로 쓴다(write 두 번). 그사이 다른 스레드의 줄이 끼어도 줄은 스레드마다 통째로 — 창에도, 오류·주의 모음에도.
        예전(한 버퍼): '주의: 요청 한도 초과 — …' 뒤에 붙은 '오류:' 줄은 '주의' 로 모여 finish_alert·실패 쪽 셈에서 빠졌다."""
        with gui_module() as gui:
            w = gui.QueueWriter(queue.Queue())
            a_mid, b_done = threading.Event(), threading.Event()
            def a():
                w.write("주의: 요청 한도 초과 — 20초 뒤 다시 시도 (1)"); a_mid.set(); b_done.wait(10); w.write("\n")
            def b():
                a_mid.wait(10); w.write("오류: 3쪽 판독 실패"); w.write("\n"); w.write("  4쪽: 영역 3"); w.write(" · 확인 필요 0\n"); b_done.set()
            ts = [threading.Thread(target=f) for f in (a, b)]
            for t in ts: t.start()
            for t in ts: t.join(30)
            got = []
            while not w.q.empty():
                got.append(w.q.get_nowait()[1])
            self.assertEqual(got, ["오류: 3쪽 판독 실패\n", "  4쪽: 영역 3 · 확인 필요 0\n", "주의: 요청 한도 초과 — 20초 뒤 다시 시도 (1)\n"])
            self.assertEqual(w.problems, ["오류: 3쪽 판독 실패", "주의: 요청 한도 초과 — 20초 뒤 다시 시도 (1)"])
            al = gui.finish_alert(3, "인식", w.problems); self.assertIn("실패한 쪽이 있습니다", al[1]); self.assertIn("오류: 3쪽 판독 실패", al[1])
            # 여러 스레드가 줄을 조각조각(print 처럼) 쓰는 경쟁: 줄은 하나도 섞이거나 사라지지 않는다
            w = gui.QueueWriter(queue.Queue())
            def go(i):
                for j in range(200):
                    if j % 3 == 0:
                        w.write(f"오류: {i}-{j}쪽"); w.write(" 판독 실패"); w.write("\n")
                    elif j % 3 == 1:
                        w.write(f"주의: 요청 한도 초과 — {i}-{j}"); w.write("\n")
                    else:
                        w.write(f"  {i}-{j}쪽: 영역 3 · "); w.write("확인 필요 0\n주의: 둘째 "); w.write(f"줄 {i}-{j}\n")
            old = sys.getswitchinterval(); sys.setswitchinterval(1e-6)
            try:
                ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
                for t in ts: t.start()
                for t in ts: t.join(60)
            finally:
                sys.setswitchinterval(old)
            want = {f"오류: {i}-{j}쪽 판독 실패" if j % 3 == 0 else (f"주의: 요청 한도 초과 — {i}-{j}" if j % 3 == 1 else f"주의: 둘째 줄 {i}-{j}")
                    for i in range(6) for j in range(200)}
            self.assertEqual(len(w.problems), len(want)); self.assertEqual(set(w.problems), want)
            chunks = []
            while not w.q.empty():
                chunks.append(w.q.get_nowait()[1])
            self.assertTrue(all(c.endswith("\n") for c in chunks))                               # 창에도 끝난 줄만
            lines = "".join(chunks).splitlines()
            self.assertEqual(len(lines), 6 * 200 + 6 * 66); self.assertEqual(set(lines) & want, want)
            self.assertTrue(all(l.startswith(("오류: ", "주의: ")) or (l.startswith("  ") and l.endswith("확인 필요 0")) for l in lines))
            # 줄바꿈 없이 flush 한 글(진행 점 등)은 창에 먼저 보내고, 오류·주의 판정은 줄이 끝날 때 통째로
            w = gui.QueueWriter(queue.Queue())
            print("주의: 부분", end="", file=w, flush=True); self.assertEqual(w.q.get_nowait()[1], "주의: 부분"); self.assertEqual(w.problems, [])
            print(" 끝", file=w, flush=True); self.assertEqual(w.q.get_nowait()[1], " 끝\n"); self.assertEqual(w.problems, ["주의: 부분 끝"])
            self.assertTrue(w.q.empty())

    def test_max_calls_value_and_options(self):
        with gui_module() as gui:
            mv = gui.max_calls_value
            self.assertEqual([mv(x) for x in ("", "  ", None, "0", "250", "1,000")], [0, 0, 0, 0, 250, 1000])
            self.assertEqual([mv(x) for x in ("많이", "-3", "2.5", "²")], [None, None, None, None])
            class Stub:
                pass
            for n in ("_read_opts", "_cap_opts", "media_res_key", "mode_key", "page_think_key", "page_mres_key", "save_settings", "memo_mode", "check_list_mode", "pdf", "_prepare_env"):
                setattr(Stub, n, getattr(gui.App, n))
            s = Stub()
            s._remember_inputs = mock.Mock()
            s.collate_policy = mock.Mock(return_value="sure")
            s.collate_memo_mode = mock.Mock(return_value="notice")
            vals = dict(v_reader="gemini", v_up="2", v_model="gemini-3-pro", v_second=True, v_off="", v_force=False, v_workers="8", v_model2="auto", v_think2=True,
                        v_chunk="8", v_mres=gui.MRES_LABELS["high"], v_mode=gui.MODE_LABELS["auto"], v_cache=True, v_ckpt="20",
                        v_think1=gui.PAGE_THINK_LABELS["medium"], v_mres1=gui.PAGE_MRES_LABELS["ultra_high"], v_maxcalls="250",
                        v_pages="", v_key="", v_docx=False, v_join=True, v_hl=True, v_pb=True, v_ctx=True, v_memo=gui.MEMO_LABELS["notice"],
                        v_chk=gui.CHECK_LIST_LABELS["none"], v_blind=False, v_pdf="")
            for k, v in vals.items():
                setattr(s, k, V(v))
            o = s._read_opts()
            self.assertEqual((o[o.index("--page-thinking") + 1], o[o.index("--page-media-resolution") + 1], o[o.index("--max-calls") + 1]), ("medium", "ultra_high", "250"))
            from yeone_ocr.cli import build_parser
            for argv in (["read", "x.pdf"], ["run", "x.pdf"], ["collate", "x.pdf", "--hwpx", "a.hwpx"]):
                a = build_parser().parse_args(argv + o)                                          # 창이 만든 선택지를 명령줄이 받는다
                self.assertEqual((a.page_thinking, a.page_media_resolution, a.max_calls), ("medium", "ultra_high", 250), argv)
            self.assertEqual(build_parser().parse_args(["adjudicate", "x.pdf"] + s._cap_opts()).max_calls, 250)
            s.v_maxcalls = V("0"); self.assertNotIn("--max-calls", s._read_opts()); self.assertEqual(s._cap_opts(), [])
            s.v_think1 = V(gui.PAGE_THINK_LABELS["auto"]); s.v_mres1 = V(gui.PAGE_MRES_LABELS["auto"])
            o = s._read_opts(); self.assertEqual((o[o.index("--page-thinking") + 1], o[o.index("--page-media-resolution") + 1]), ("auto", "auto"))
            s.cfg = {"max_calls": 99}; s.v_maxcalls = V("250"); s.v_think1 = V(gui.PAGE_THINK_LABELS["high"])
            with mock.patch.object(gui, "save_config"):
                s.save_settings(quiet=True)
            self.assertEqual((s.cfg["page_thinking"], s.cfg["page_media_res"], s.cfg["max_calls"]), ("high", "auto", 250))
            s.v_maxcalls = V("많이")
            with mock.patch.object(gui, "save_config"):
                s.save_settings(quiet=True)
            self.assertEqual(s.cfg["max_calls"], 250)                                            # 잘못 넣은 값은 저장하지 않는다
            s.v_reader = V("mock"); saved = []
            s.save_settings = lambda quiet=False: saved.append(quiet)
            with mock.patch.object(gui, "messagebox") as mb:
                self.assertFalse(s._prepare_env())                                               # 실행 전에 막는다(상한 없음으로 잘못 읽지 않게)
            self.assertTrue(mb.showwarning.called); self.assertEqual(saved, [])
            s.v_maxcalls = V("300")
            self.assertTrue(s._prepare_env()); self.assertEqual(saved, [True])

    def test_table_rows_parsed_off_main_thread_and_stale_results_dropped(self):
        with gui_module() as gui, tempfile.TemporaryDirectory() as td:
            w = Path(td) / "b_YeonE_OCR"; (w / "readings").mkdir(parents=True)
            (w / "state.json").write_text(json.dumps({"total_pages": 3, "pages": {"1": {"statuses": ["1차 OCR 완료", "판독 확인 필요"]}, "3": {"statuses": []}}}), encoding="utf-8")
            (w / "readings" / "p001.json").write_text(json.dumps({"merged": {"print_page": "30", "uncertain": [{}, {}], "regions": [{"kind": "본문"}, {"kind": "삽화"}, {"kind": "산도"}]}}), encoding="utf-8")
            (w / "readings" / "p002.json").write_text("{깨진", encoding="utf-8")
            rows = [(1, "30", "1차 OCR 완료 · 판독 확인 필요", 2, 2), (2, "", "미처리", "", ""), (3, "", "미처리", "", "")]
            self.assertEqual(gui.table_rows(w), rows)
            self.assertEqual(gui.table_rows(Path(td) / "없음"), [])

            class Table:
                def __init__(self):
                    self.items = {}; self.n = 0
                def get_children(self):
                    return tuple(self.items)
                def delete(self, i):
                    del self.items[i]
                def insert(self, parent, index, values=(), tags=()):
                    self.n += 1; self.items[self.n] = tuple(values)
            cur = {"pdf": str(Path(td) / "b.pdf"), "work": w}
            app = types.SimpleNamespace(table=Table(), q=queue.Queue(), pdf=lambda: cur["pdf"], work=lambda: cur["work"])
            app._fill_table = lambda *a: gui.App._fill_table(app, *a)
            on_main = []
            real_rows = gui.table_rows
            def spy(wd):
                on_main.append(threading.current_thread() is threading.main_thread()); return real_rows(wd)
            with mock.patch.object(gui, "table_rows", spy):
                gui.App.refresh_table(app)
                first = app.q.get(timeout=20)
                gui.App.refresh_table(app)                                                       # 같은 PDF 를 한 번 더(앞 결과가 늦게 온 경우)
                second = app.q.get(timeout=20)
            self.assertEqual(on_main, [False, False])                                            # 기록 읽기는 작업 스레드에서
            self.assertEqual((first[0], first[2], first[3]), ("table", cur["pdf"], rows))
            gui.App._fill_table(app, *first[1:]); self.assertEqual(list(app.table.items.values()), [])      # 더 새 요청이 있으니 버림
            gui.App._fill_table(app, *second[1:]); self.assertEqual(list(app.table.items.values()), rows)
            gui.App.refresh_table(app); late = app.q.get(timeout=20)                             # 그사이 PDF 를 바꾸면
            cur["pdf"] = str(Path(td) / "c.pdf"); cur["work"] = Path(td) / "c_YeonE_OCR"
            gui.App._fill_table(app, *late[1:]); self.assertEqual(list(app.table.items.values()), rows)       # 옛 PDF 의 늦은 줄은 버림
            gui.App.refresh_table(app)
            self.assertEqual(list(app.table.items.values()), [])                                 # 다른 PDF 로 바뀌면 표는 바로 비운다
            gui.App._fill_table(app, *app.q.get(timeout=20)[1:]); self.assertEqual(list(app.table.items.values()), [])
            cur["pdf"] = ""
            gui.App.refresh_table(app); self.assertTrue(app.q.empty())                           # PDF 가 없으면 읽지 않는다


def _real_tk():
    try:
        import _tkinter  # noqa: F401
        import tkinter
        root = tkinter.Tk()
    except Exception as e:
        raise unittest.SkipTest(f"tkinter·화면 없음: {e}")
    root.withdraw()
    return root


class GuiOpsRealTk(unittest.TestCase):
    """진짜 tkinter 가 있을 때(xvfb): 표 채우기와 창 전체(환율 하루 한 번·쪽별 상태 작업 스레드)."""
    def test_fill_table_on_real_treeview(self):
        root = _real_tk()
        try:
            from tkinter import ttk
            with gui_module() as gui:
                tree = ttk.Treeview(root, columns=("pdf", "print", "status", "unc", "figs"), show="headings")
                app = types.SimpleNamespace(table=tree, pdf=lambda: "b.pdf", _table_gen=2)
                gui.App._fill_table(app, 2, "b.pdf", [(1, "30", "완료", 2, 1), (2, "", "미처리", "", "")])
                self.assertEqual([tree.item(i)["values"] for i in tree.get_children()], [[1, 30, "완료", 2, 1], [2, "", "미처리", "", ""]])
                self.assertEqual(tree.item(tree.get_children()[0])["tags"], ["odd"])
                gui.App._fill_table(app, 1, "b.pdf", [(9, "", "옛 줄", "", "")])               # 늦게 온 옛 결과
                self.assertEqual(len(tree.get_children()), 2); self.assertEqual(app._table_pdf, "b.pdf")
        finally:
            root.destroy()

    def test_app_fetches_fx_once_a_day_and_fills_table_from_worker(self):
        _real_tk().destroy()
        with gui_module() as gui, tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "cfg" / "config.json"; cfgp.parent.mkdir()
            fetched = []
            def make(day):
                cfgp.write_text(json.dumps({"fx_auto": 1385.5, "fx_auto_date": day}), encoding="utf-8")
                return gui.App()
            with mock.patch.object(gui, "CONFIG_PATH", cfgp), mock.patch.object(gui, "missing_packages", lambda: []), \
                    mock.patch.object(gui.App, "_fetch_fx", lambda self: fetched.append(1)):
                app = make(time.strftime("%Y-%m-%d"))
                try:
                    self.assertEqual(app._fx, (1385.5, "오늘 자동 조회")); time.sleep(0.2); self.assertEqual(fetched, [])
                    pdf = Path(td) / "b.pdf"; pdf.write_bytes(b"%PDF-1.4\n")
                    w = gui.work_dir_for(str(pdf)); (w / "readings").mkdir(parents=True)
                    (w / "state.json").write_text(json.dumps({"total_pages": 2, "pages": {"1": {"statuses": ["1차 OCR 완료"]}}}), encoding="utf-8")
                    app.v_pdf.set(str(pdf)); app.refresh_table()
                    t0 = time.time()
                    while time.time() - t0 < 10 and not app.table.get_children():
                        app.update(); time.sleep(0.02)
                    self.assertEqual([app.table.item(i)["values"][2] for i in app.table.get_children()], ["1차 OCR 완료", "미처리"])
                finally:
                    app.destroy()
                app = make("2026-01-02")
                try:
                    self.assertEqual(app._fx, (1385.5, "2026-01-02 자동 조회"))
                    t0 = time.time()
                    while not fetched and time.time() - t0 < 5:
                        time.sleep(0.02)
                    self.assertEqual(fetched, [1])                                               # 지난날 값이면 한 번 조회
                finally:
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
