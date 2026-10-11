"""작업 폴더와 쪽별 상태·판독 기록. '완료'는 실제 수행한 범위에만 붙는다(지침 33)."""
from __future__ import annotations
import json, os, shutil, time, threading
from pathlib import Path

STATUSES = ["미처리", "영역 분류 완료", "1차 OCR 완료", "2차 독립 판독 비교 완료", "판독 확인 필요",
            "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료", "원본 대조 완료(사람)"]
HUMAN_ONLY = {"원본 대조 완료(사람)"}   # 프로그램이 스스로 붙이지 않는 상태

WARN = lambda msg: print(msg, flush=True)      # 깨진 파일을 치웠을 때의 한 줄 알림('주의:'). 창 프로그램은 stdout 을 모으므로 그대로 보인다


def _tmp_path(path: Path) -> Path:
    """같은 폴더의 숨긴 임시 이름(스레드마다 다르게). 이름이 '.' 으로 시작해 p*.json 같은 찾기에 걸리지 않는다."""
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write_atomic_with(path: Path, writer) -> None:
    """writer(임시 경로) 로 다 쓴 뒤 os.replace 로 바꿔 넣는다(Windows·POSIX 모두 원자적) — 도중에 죽어도 쓰다 만 파일이 남지 않는다."""
    path = Path(path); tmp = _tmp_path(path)
    try:
        writer(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass


def write_atomic(path: Path, data: str | bytes) -> None:
    write_atomic_with(path, (lambda t: t.write_bytes(data)) if isinstance(data, bytes) else (lambda t: t.write_text(data, encoding="utf-8")))


def free_name(path: Path) -> Path:
    """path 가 비어 있으면 그대로, 있으면 확장자 앞에 시각을 붙인 새 이름(pNNN.20261011-153000.json, 같은 초면 -2, -3 …) — 앞서 남긴 사본을 덮지 않게."""
    path = Path(path)
    if not path.exists():
        return path
    ts = time.strftime("%Y%m%d-%H%M%S"); k = 1
    while True:
        q = path.with_name(f"{path.stem}.{ts}{'' if k == 1 else f'-{k}'}{path.suffix}")
        if not q.exists():
            return q
        k += 1


def default_work_dir(pdf_path: str) -> Path:
    """--work 가 없을 때의 결과 폴더. 창의 work_dir_for 와 같은 규칙: PDF 는 work/<이름>, 이미지는 work/<이름>_<확장자>,
    이미지 폴더는 work/<폴더>_이미지(같은 이름끼리 판독 기록이 섞이지 않게)."""
    p = Path(pdf_path)
    name = p.absolute().name + "_이미지" if p.is_dir() else (f"{p.stem}_{p.suffix.lower().lstrip('.')}" if p.suffix.lower() not in ("", ".pdf") else p.stem)
    return Path("work") / name


def legacy_work_dir(pdf_path: str) -> Path | None:
    """0.6.4 전 판이 이미지·이미지 폴더에 쓰던 결과 폴더 work/<이름>(확장자·'_이미지' 없이). 새 기본 폴더는 아직 없는데 그 옛 폴더에
    이 입력의 기록(state.json 의 file 이름이 같음)이 있을 때만 돌려준다. 저절로 옮겨 쓰지는 않는다(사용자가 --work 로 고르게)."""
    p = Path(pdf_path); new = default_work_dir(pdf_path); old = Path("work") / p.stem
    if not (p.stem and old != new and not new.exists() and (old / "state.json").is_file()):
        return None
    try:
        f = json.loads((old / "state.json").read_text(encoding="utf-8")).get("file")
    except (OSError, ValueError, AttributeError):
        f = None
    return old if f in (None, p.name) else None                       # 같은 이름의 PDF 결과(work/<이름>)는 가리키지 않는다


class Work:
    _lock = threading.RLock()      # 여러 쪽을 동시에 판독할 때 state.json 읽기-고치기-쓰기가 겹치지 않게

    def __init__(self, pdf_path: str, work_dir: str | None = None):
        self.pdf_path = Path(pdf_path)
        self.dir = Path(work_dir) if work_dir else default_work_dir(pdf_path)
        self.pages_dir = self.dir / "pages"
        self.textlayer_dir = self.dir / "textlayer"
        self.readings_dir = self.dir / "readings"
        self.crops_dir = self.dir / "crops"
        self.out_dir = self.dir / "out"
        for d in (self.pages_dir, self.textlayer_dir, self.readings_dir, self.crops_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"

    # ---- 상태 ----
    def load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8-sig"))      # -sig: 메모장이 붙인 BOM 도 받는다
            except ValueError:                     # 쓰다 만 JSON(도중에 죽음) — 치우고 새로 시작한다. 판독 기록(readings/)은 그대로
                bad = free_name(self.state_path.with_name("state.json.broken"))     # 앞서 치운 것은 덮지 않는다
                try: os.replace(self.state_path, bad)
                except OSError: pass
                WARN(f"주의: {self.state_path} 이(가) 깨져 있어(쓰다 만 JSON) {bad.name} 으로 옮기고 상태를 새로 시작합니다. 판독 기록은 그대로입니다.")
        return {"file": self.pdf_path.name, "pages": {}}

    def save_state(self, st: dict) -> None:
        write_atomic(self.state_path, json.dumps(st, ensure_ascii=False, indent=1))

    def set_status(self, page: int, status: str, note: str | None = None, remove: list[str] | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"알 수 없는 상태: {status}. 가능: {STATUSES}")
        with self._lock:
            self._set_status_locked(page, status, note, remove)

    def _set_status_locked(self, page, status, note, remove):
        st = self.load_state()
        p = st["pages"].setdefault(str(page), {"statuses": ["미처리"], "notes": []})
        s = [x for x in p["statuses"] if x != "미처리" and x not in (remove or [])]
        if status not in s:
            s.append(status)
        p["statuses"] = s
        if note:
            p["notes"].append(note)
        p["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save_state(st)

    def ensure_pages(self, pages: list[int], total_pages: int | None = None) -> None:
        """쪽들을 상태에 등록한다(있으면 그대로). 잠금 안에서 한 번의 읽기-고치기-쓰기 — 판독 스레드의 set_status 와 겹치지 않게."""
        with self._lock:
            st = self.load_state()
            for n in pages:
                st["pages"].setdefault(str(n), {"statuses": ["미처리"], "notes": []})
            if total_pages is not None:
                st["total_pages"] = int(total_pages)
            self.save_state(st)

    def statuses(self, page: int) -> list[str]:
        return self.load_state()["pages"].get(str(page), {}).get("statuses", ["미처리"])

    # ---- 기록 ----
    def record_path(self, page: int) -> Path:
        return self.readings_dir / f"p{page:03d}.json"

    def _read_record(self, p: Path) -> dict | None:
        """기록 파일 하나. 쓰다 만 JSON 이면 readings/broken/ 으로 옮기고 None(그 쪽은 안 읽은 것으로) — 결과 폴더 전체가 못 쓰게 되지 않게.
        손으로 고치다 BOM 이 붙은 기록은 깨진 것이 아니다(utf-8-sig). 앞서 broken/ 에 옮긴 같은 쪽 파일은 덮지 않는다."""
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except ValueError:
            bd = self.readings_dir / "broken"; bd.mkdir(exist_ok=True)
            dst = free_name(bd / p.name)
            try: os.replace(p, dst)
            except OSError: pass
            WARN(f"주의: 판독 기록 {p.name} 이(가) 깨져 있어(쓰다 만 JSON) readings/broken/{dst.name} 으로 옮겼습니다. 그 쪽은 다시 판독합니다.")
            return None

    def load_record(self, page: int) -> dict | None:
        p = self.record_path(page)
        return self._read_record(p) if p.exists() else None

    def save_record(self, page: int, rec: dict) -> None:
        write_atomic(self.record_path(page), json.dumps(rec, ensure_ascii=False, indent=1))

    def backup_record(self, page: int) -> Path | None:
        """기록을 readings/prev/ 에 복사해 둔다. 다시 비교·다시 판독으로 덮어쓰기 전에 — 손으로 고친 merged 가 남게.
        처음은 prev/pNNN.json, 그 뒤 내용이 다르면 prev/pNNN.<시각>.json 으로 하나 더(앞의 사본을 덮지 않는다 — 첫 사본 뒤에 고친 글도 남게).
        돌려주는 값은 이번에 쓴 사본 경로, 기록이 없거나 같은 내용의 사본이 이미 있으면 None."""
        src = self.record_path(page)
        if not src.exists():
            return None
        pd = self.readings_dir / "prev"; pd.mkdir(exist_ok=True)
        data = src.read_bytes()
        for old in [pd / src.name, *pd.glob(f"{src.stem}.*{src.suffix}")]:
            if old.is_file() and old.read_bytes() == data:
                return None
        dst = free_name(pd / src.name)
        write_atomic_with(dst, lambda t: shutil.copy2(src, t))
        return dst

    def page_image_path(self, page: int) -> Path:
        return self.pages_dir / f"p{page:03d}.png"

    def textlayer_path(self, page: int) -> Path:
        return self.textlayer_dir / f"p{page:03d}.json"

    def records(self, pages: list[int] | None = None) -> list[dict]:
        out = []
        for p in sorted(self.readings_dir.glob("p*.json")):
            rec = self._read_record(p)
            if rec is None:
                continue
            if pages is None or rec["pdf_page"] in pages:
                out.append(rec)
        return sorted(out, key=lambda r: r["pdf_page"])
