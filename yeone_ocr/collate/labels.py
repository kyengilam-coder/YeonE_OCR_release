"""책갈피 교정의 고르기 이름표. 창 프로그램이 패키지(lxml 등) 없이도 뜨도록 무거운 모듈과 따로 둔다."""

POLICIES = {"memo": "표시만(글자 안 바꿈)", "sure": "확실한 것만(A) 자동", "all": "전부 자동(A·B2), B1·C 는 표시"}
ANCHOR_SOURCES = {"auto": "자동 (HWPX 책갈피·제목 → 없으면 PDF 책갈피로 본문 검색)", "hwpx": "HWPX 책갈피·제목 스타일", "pdf": "PDF 책갈피 이름으로 HWPX 본문 검색"}
MEMO_MODES = {"none": "색상만 표시(기본)", "notice": "고지 사항만 메모", "all": "모든 표시에 메모(근거, 이전 방식)", "inline": "글 속 〔…〕 표시(이전 방식)"}
# 일반·이미지 OCR 의 문서 메모. none 은 메모도 글 속 고지도 넣지 않는다(색·〔〕 표기는 그대로 — 고지는 검수 화면에)
GENERAL_MEMO_MODES = {"none": MEMO_MODES["none"], "notice": "고지 사항만 메모", "all": MEMO_MODES["all"], "inline": "메모 없이 글 속 〔…〕"}
