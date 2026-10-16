"""이미지 OCR 에 추가한 이미지·기존 묶음의 쪽을 입력 순서대로 합친다."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf

from . import pdfio


def save_sources_as_pdf(paths: list[str | os.PathLike], out_path: str | os.PathLike, progress=None) -> str:
    """이미지와 PDF 의 모든 쪽을 주어진 순서대로 저장하고 원본 파일은 보존한다.

    이미지의 방향·해상도·여러 쪽 TIFF 처리는 기존 이미지 입력과 같다. PDF 는
    렌더링하지 않고 쪽을 복사한다. progress(끝난 소스 수, 전체 소스 수) 는 소스마다
    호출한다. 모든 소스를 읽은 뒤 저장하므로 읽기 실패 시 기존 출력도 그대로 둔다.
    창 프로그램은 임시 출력에 저장한 뒤 최종 경로로 바꾸어 쓰기 실패도 보호한다.
    """
    destination = Path(out_path)
    if any(Path(path).resolve() == destination.resolve() for path in paths):
        raise ValueError("원본 파일과 다른 경로에 이미지 묶음을 저장해야 합니다")
    with pymupdf.open() as combined:
        for index, path in enumerate(paths):
            with pdfio.open_pdf(str(path)) as source:
                combined.insert_pdf(source)
            if progress:
                progress(index + 1, len(paths))
        if not len(combined):
            raise ValueError("이미지가 없습니다")
        data = combined.tobytes(garbage=0, deflate=False)
    destination.write_bytes(data)
    return str(out_path)
