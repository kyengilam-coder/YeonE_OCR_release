"""작업 폴더와 쪽별 상태·판독 기록. '완료'는 실제 수행한 범위에만 붙는다(지침 33)."""
from __future__ import annotations
import json, time
from pathlib import Path

STATUSES = ["미처리", "영역 분류 완료", "1차 OCR 완료", "2차 독립 판독 비교 완료", "판독 확인 필요",
            "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료", "원본 대조 완료(사람)"]
HUMAN_ONLY = {"원본 대조 완료(사람)"}   # 프로그램이 스스로 붙이지 않는 상태


class Work:
    def __init__(self, pdf_path: str, work_dir: str | None = None):
        self.pdf_path = Path(pdf_path)
        self.dir = Path(work_dir) if work_dir else Path("work") / self.pdf_path.stem
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
