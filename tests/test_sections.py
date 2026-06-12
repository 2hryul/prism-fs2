"""비주석 본문 섹션 추출(sections.py, 검색개선 Phase3) 단위테스트.

가짜 doc(page_count/__getitem__)으로 결정론 검증 — fitz 실 PDF 불요.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import sections as sec  # noqa: E402


class _FakePage:
    def __init__(self, text):
        self._t = text

    def get_text(self):
        return self._t


class _FakeDoc:
    def __init__(self, pages):
        self._pages = [_FakePage(t) for t in pages]
        self.page_count = len(pages)

    def __getitem__(self, i):
        return self._pages[i]


def _doc_with_sections():
    """목차(점선)+본문 로마자 섹션 + 재무페이지 로마자 주석 헤더(오판 유발원) 구성."""
    pages = ["표지"]
    # p2: 목차 — 점선 리더 라인들(헤더 4개 이상 → 앞부분 TOC 판정)
    pages.append("목차\nI. 회사의 개요......3\nII. 사업의 내용......5\n"
                 "III. 재무에 관한 사항......8\nIV. 경영진단......12\n")
    pages.append("I. 회사의 개요\n회사는 지주회사로서 " + "내용 " * 100)   # p3
    pages.append("개요 후속 페이지 " + "내용 " * 100)                      # p4
    pages.append("II. 사업의 내용\n영업 개황 " + "내용 " * 100)            # p5
    pages.append("사업 후속 " + "내용 " * 100)                            # p6
    pages.append("계속 " + "내용 " * 100)                                 # p7
    # p8: III + 같은 페이지에 로마자 주석류 헤더 다수(전역 TOC 판정이면 III 누락되는 케이스)
    pages.append("III. 재무에 관한 사항\nX. 당기법인세\nXI. 보험계약부채\n"
                 "XII. 재보험\nXIII. 투자계약부채\n" + "재무 " * 100)
    pages.append("재무 본문 " + "내용 " * 100)                            # p9
    return _FakeDoc(pages)


def test_extract_sections_skips_toc_and_keeps_run():
    doc = _doc_with_sections()
    secs = sec.extract_sections(doc)
    titles = [s["title"] for s in secs]
    assert any("회사의 개요" in t for t in titles)
    assert any("사업의 내용" in t for t in titles)
    # 본문 재무 페이지(p8)의 로마자 주석 헤더에도 불구하고 III 채택(전역 TOC 판정 아님)
    assert any("재무에 관한 사항" in t for t in titles)
    # 목차 페이지(p2)의 점선 라인은 섹션 시작으로 오인하지 않음
    assert all(s["page_start"] != 2 for s in secs)
    # 점선 리더가 제목에 남지 않음
    assert all(".." not in s["title"] for s in secs)


def test_section_ranges_are_contiguous():
    doc = _doc_with_sections()
    secs = sec.extract_sections(doc)
    for a, b in zip(secs, secs[1:]):
        assert a["page_end"] == b["page_start"] - 1
    assert secs[-1]["page_end"] == doc.page_count


def test_plan_chunks_excludes_covered_pages():
    doc = _doc_with_sections()
    s = {"page_start": 5, "page_end": 7}
    chunks_all = sec.plan_section_chunks(doc, s, set(), lambda t: [t[:200]])
    chunks_cov = sec.plan_section_chunks(doc, s, {6}, lambda t: [t[:200]])
    assert {c["page"] for c in chunks_all} == {5, 6, 7}
    assert {c["page"] for c in chunks_cov} == {5, 7}  # 주석 커버 페이지 제외


def test_plan_chunks_per_page_spread():
    """페이지당 할당량 — 첫 페이지가 섹션 예산을 독식하지 않음."""
    doc = _FakeDoc(["가" * 5000, "나" * 5000])
    s = {"page_start": 1, "page_end": 2}
    many = lambda t: [t[i:i + 200] for i in range(0, len(t), 200)]
    chunks = sec.plan_section_chunks(doc, s, set(), many)
    per_page = {}
    for c in chunks:
        per_page[c["page"]] = per_page.get(c["page"], 0) + 1
    assert per_page[1] <= sec.MAX_CHUNKS_PER_SECTION_PAGE
    assert 2 in per_page  # 둘째 페이지도 청크 확보


def test_covered_pages_caps_trailing_note_artifact():
    """마지막 주석이 문서 끝까지 확장되는 아티팩트 — 캡으로 후속 본문을 살림."""
    notes = [{"page_start": 100, "page_end": 600}]  # 비정상 500p 범위
    covered = sec.covered_pages_from_notes(notes)
    assert 100 in covered
    assert 100 + sec._COVER_PAGES_PER_NOTE_CAP - 1 in covered
    assert 200 not in covered and 600 not in covered


def test_roman_to_int():
    assert sec._roman_to_int("I") == 1
    assert sec._roman_to_int("IV") == 4
    assert sec._roman_to_int("XII") == 12
    assert sec._roman_to_int("ABC") == 0