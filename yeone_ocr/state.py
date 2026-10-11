"""작업 폴더와 쪽별 상태·판독 기록. '완료'는 실제 수행한 범위에만 붙는다(지침 33)."""
from __future__ import annotations
import json, time, threading
from pathlib import Path

STATUSES = ["미처리", "영역 분류 완료", "1차 OCR 완료", "2차 독립 판독 비교 완료", "판독 확인 필요",
            "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료", "원본 대조 완료(사람)"]
HUMAN_ONLY = {"원본 대조 완료(사람)"}   # 프로그램이 스스로 붙이지 않는 상태


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
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"file": self.pdf_path.name, "pages": {}}

    def save_state(self, st: dict) -> None:
        self.state_path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

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

    def statuses(self, page: int) -> list[str]:
        return self.load_state()["pages"].get(str(page), {}).get("statuses", ["미처리"])

    # ---- 기록 ----
    def record_path(self, page: int) -> Path:
        return self.readings_dir / f"p{page:03d}.json"

    def load_record(self, page: int) -> dict | None:
        p = self.record_path(page)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_record(self, page: int, rec: dict) -> None:
        self.record_path(page).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")

    def page_image_path(self, page: int) -> Path:
        return self.pages_dir / f"p{page:03d}.png"

    def textlayer_path(self, page: int) -> Path:
        return self.textlayer_dir / f"p{page:03d}.json"

    def records(self, pages: list[int] | None = None) -> list[dict]:
        out = []
        for p in sorted(self.readings_dir.glob("p*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            if pages is None or rec["pdf_page"] in pages:
                out.append(rec)
        return sorted(out, key=lambda r: r["pdf_page"])
