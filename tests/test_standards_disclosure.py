"""회계기준서 목차 기반 '공시' 섹션 추출 단위테스트 (PDF 불요 — pages 합성)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def _toc_pages():
    """제1016호 목차 추출 형태 모사: 제목 열 전체 → '문단번호' → 범위 열 전체(순서동기)."""
    toc = ["- 5 -", "목  차", "기업회계기준서 제1016호 '유형자산'",
           "목적", "적용", "공시", "경과규정",
           "문단번호",
           "1", "한2.1", "73~79", "80~80D"]
    body_p29 = ["- 29 -", "공시", "73", "기업은 다음 사항을 공시한다."]
    return [(1, ["표지"]), (5, toc), (29, body_p29)]


def test_parse_toc_disclosure_pairs_by_order():
    # 제목[2]='공시' ↔ 범위[2]='73~79' → 73~79
    assert app._parse_toc_disclosure(_toc_pages()) == {"para_start": "73", "para_end": "79"}


def test_resolve_section_page_uses_heading():
    # 본문 '공시' 헤딩 라인 페이지 = 29
    assert app._resolve_section_page(_toc_pages(), "73") == 29


def test_compute_disclosure_full():
    assert app._compute_standard_disclosure(_toc_pages()) == {
        "page_start": 29, "para_start": "73", "para_end": "79"}


def test_no_disclosure_section_returns_none():
    # '공시' 섹션 없음 → TOC None, 본문 헤딩/앵커 없음 → 전체 None
    pages = [(5, ["목  차", "목적", "적용", "문단번호", "1", "2~5"]),
             (10, ["- 10 -", "본문 텍스트"])]
    assert app._parse_toc_disclosure(pages) is None
    assert app._compute_standard_disclosure(pages) is None


def test_toc_count_mismatch_falls_back_to_page_only():
    # 제목·범위 개수 불일치(페어링 실패)지만 본문 '공시' 헤딩 있으면 page_start만.
    pages = [(5, ["목  차", "목적", "공시", "문단번호", "1"]),   # 제목3 vs 범위1
             (40, ["- 40 -", "공시", "내용"])]
    assert app._parse_toc_disclosure(pages) is None
    assert app._compute_standard_disclosure(pages) == {"page_start": 40}
