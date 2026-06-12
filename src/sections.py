# -*- coding: utf-8 -*-
"""sections.py — 비주석 본문 섹션 추출 (검색개선 Phase3) · prism-fs

DART 분기/사업보고서 본문의 로마숫자 목차(I. 회사의 개요 / II. 사업의 내용 /
III. 재무에 관한 사항 …)를 헤더 휴리스틱으로 추출해, 주석 인덱스가 커버하지 않는
페이지(재무제표 본표·MD&A·사업내용 등)를 섹션 단위로 검색 가능하게 한다.

설계 원칙:
- 주석(index.json notes)이 이미 커버한 페이지는 청크에서 제외 → 중복 결과 없음.
  섹션 인덱스는 "주석 밖" 콘텐츠 전용 보완 축.
- 임베딩은 호출부(app)가 수행(비동기·모델 보유) — 이 모듈은 순수 추출/청크 계획만.
- AI/LLM 미사용(정규식+규칙). 실패 시 빈 결과(무회귀).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

# 페이지 상단부에서 "I. 제목" 형태 헤더 탐지. 로마숫자 I~XX, 제목은 한글/영문 시작.
_SECTION_HEADER_RE = re.compile(
    r"^\s*([IVX]{1,5})\s*\.\s*([가-힣A-Za-z][^\n]{1,40})", re.MULTILINE)

# 목차(TOC) 점선 리더("....") 포함 라인 — 본문 헤더가 아니라 목차 항목.
_TOC_LEADER_RE = re.compile(r"\.{2,}")

# 문서 앞부분(표지·목차 영역)에서 헤더가 이 수 이상 몰린 페이지는 목차로 간주.
_TOC_PAGE_MIN_HEADERS = 4
_TOC_FRONT_PAGES = 15  # 목차 페이지 판정을 적용할 앞부분 범위
                       # (본문 재무 페이지의 로마자 주석 헤더 오판 방지 — 라이브 실측)

# 런 진행 시 허용 갭 — 일부 섹션 헤더가 페이지 꼬리에 있어 누락돼도 다음 섹션으로 연결.
_RUN_MAX_GAP = 2

# 섹션당 청크 상한 — 섹션은 주석보다 범위가 넓어 별도 예산(인덱스 비대 방지).
MAX_CHUNKS_PER_SECTION = 150
# 페이지당 청크 상한 — 긴 섹션(사업의 내용 수십 p)이 앞 페이지에서 예산을 소진하지
# 않고 전 범위에 분산되도록(주석 청킹과 동일 원칙).
MAX_CHUNKS_PER_SECTION_PAGE = 3

_ROMAN = {"I": 1, "V": 5, "X": 10}


def _roman_to_int(s: str) -> int:
    """로마숫자(I~XX 범위) → 정수. 비정상 입력은 0."""
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN.get(ch, 0)
        if v == 0:
            return 0
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def extract_sections(doc) -> List[Dict[str, Any]]:
    """PDF 에서 로마숫자 본문 섹션 경계를 추출.

    Args:
        doc: fitz.Document(또는 page_count/__getitem__ 동형 대역)
    Returns:
        [{"no": "S1", "title": "I. 회사의 개요", "page_start", "page_end"}]
        — 번호 단조 증가 시퀀스만 채택(본문 등장 순). 탐지 실패 시 빈 리스트.
    """
    headers: List[Dict[str, Any]] = []
    for p in range(doc.page_count):
        text = doc[p].get_text() or ""
        # 페이지 상단부(처음 600자)만 — 섹션 시작 페이지의 머리 헤더를 노림.
        matches = _SECTION_HEADER_RE.findall(text[:600])
        # 목차 페이지 판정: 문서 앞부분 + 헤더 다수 + 점선 리더 동반 시에만 스킵.
        # (본문 재무 페이지의 로마자 주석 헤더 X.~XIV. 는 점선이 없어 오판하지 않는다.)
        if p < _TOC_FRONT_PAGES:
            all_matches = _SECTION_HEADER_RE.findall(text)
            dotted = sum(1 for _r, t in all_matches if _TOC_LEADER_RE.search(t))
            if len(all_matches) >= _TOC_PAGE_MIN_HEADERS and dotted >= 2:
                continue
        for roman, title in matches:
            if _TOC_LEADER_RE.search(title):
                continue  # 점선 리더 포함 → 목차 항목 라인
            num = _roman_to_int(roman)
            if num:
                headers.append({"num": num, "title": f"{roman}. {title.strip()}",
                                "page": p + 1})

    # 번호 증가 런 선택(첫 등장 우선) — 갭 허용(헤더가 페이지 꼬리에 있어 누락된 경우 연결).
    run: List[Dict[str, Any]] = []
    for h in headers:
        if not run:
            if h["num"] == 1:
                run.append(h)
        elif run[-1]["num"] < h["num"] <= run[-1]["num"] + _RUN_MAX_GAP:
            run.append(h)
    if len(run) < 2:
        return []

    sections = []
    for i, h in enumerate(run):
        page_end = (run[i + 1]["page"] - 1) if i + 1 < len(run) else doc.page_count
        sections.append({"no": f"S{h['num']}", "title": h["title"],
                         "page_start": h["page"], "page_end": max(page_end, h["page"])})
    return sections


def plan_section_chunks(doc, section: Dict[str, Any], covered_pages: Set[int],
                        chunk_text_fn) -> List[Dict[str, Any]]:
    """섹션 페이지 범위에서 '주석 미커버 페이지만' 청크 계획 생성.

    Args:
        covered_pages: 주석 인덱스가 이미 커버한 페이지 집합(중복 인덱싱 방지).
        chunk_text_fn: 텍스트 → [str] 청크 분할 함수(app._chunk_text 주입).
    Returns: [{"text", "page"}] (임베딩·토큰은 호출부 부착)
    """
    out: List[Dict[str, Any]] = []
    for p in range(section["page_start"], section["page_end"] + 1):
        if p in covered_pages or not (1 <= p <= doc.page_count):
            continue
        per_page = 0  # 페이지별 할당량 — 긴 섹션 전 범위에 청크 분산
        for c in chunk_text_fn(doc[p - 1].get_text()):
            if len(c.strip()) < 20:
                continue
            out.append({"text": c, "page": p})
            per_page += 1
            if len(out) >= MAX_CHUNKS_PER_SECTION:
                return out
            if per_page >= MAX_CHUNKS_PER_SECTION_PAGE:
                break
    return out


# 커버리지 계산용 노트당 페이지 상한 — 주석 추출기의 "마지막 노트=문서 끝까지" 규칙이
# MD&A 등 후속 본문 수백 페이지를 주석 범위로 흡수하는 아티팩트 방어(실제 주석은 ~30p 이내).
_COVER_PAGES_PER_NOTE_CAP = 30


def covered_pages_from_notes(notes: List[Dict[str, Any]]) -> Set[int]:
    """주석 노트들의 페이지 범위 합집합 — 섹션 청크에서 제외할 페이지(노트당 캡 적용)."""
    covered: Set[int] = set()
    for n in notes or []:
        ps, pe = n.get("page_start"), n.get("page_end") or n.get("page_start")
        if ps:
            covered.update(range(ps, min(pe, ps + _COVER_PAGES_PER_NOTE_CAP - 1) + 1))
    return covered
