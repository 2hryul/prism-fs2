"""report(사업보고서) 3번째 문서유형 — 경로·검출·카탈로그 필드·strip 단위테스트.

검증 범위:
- validate_doc_type("report") 통과 / pdf_path·index_path 매핑(report.pdf, index_report.json)
- _filename_matches_doc_type / detect_doc_type_from_text 의 report 판정(검토·감사 배제)
- _strip_doc_fields 가 report_* 만 제거하고 report_nm(DART 메타)·review_*·review_sep_* 보존
- _doc_indexed("report") 플래그
- safe_original_filename 이 report.pdf 작업본명을 원본으로 오인하지 않음
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def test_validate_and_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "LIBRARY_ROOT", tmp_path)
    assert app.validate_doc_type("report") == "report"
    assert "report" in app.VALID_DOC_TYPES
    c, p = "신한", "2025FY"
    assert app.pdf_path(c, p, "report").name == "report.pdf"
    assert app.index_path(c, p, "report").name == "index_report.json"


def test_filename_matches_report():
    # 사업/분기/반기보고서 & 검토·감사 미포함 → report
    assert app._filename_matches_doc_type("[신한금융]사업보고서(2025.12).pdf", "report")
    assert app._filename_matches_doc_type("[KB금융]분기보고서(2025.09).pdf", "report")
    # 검토/감사 포함 시 report 아님(검토보고서와 구분)
    assert not app._filename_matches_doc_type("[신한금융]연결검토보고서.pdf", "report")
    assert not app._filename_matches_doc_type("[신한금융]감사보고서.pdf", "report")


def test_detect_doc_type_report():
    assert app.detect_doc_type_from_text("[하나금융]사업보고서(2025.12).pdf") == "report"
    # 사업보고서지만 검토/감사 키워드 동반 → report 로 오인하지 않음
    assert app.detect_doc_type_from_text("사업보고서 중 연결검토보고서") == "review"


def test_strip_doc_fields_report_preserves_others():
    entry = {
        "company": "신한", "period": "2025FY",
        "report_indexed": True, "report_notes_count": 12, "report_collected": True,
        "report_nm": "사업보고서",                       # DART 메타 — 보존되어야 함
        "review_indexed": True, "review_notes_count": 30,
        "review_sep_indexed": True, "review_sep_notes_count": 25,
    }
    out = app._strip_doc_fields(entry, "report")
    # report_* 제거
    assert "report_indexed" not in out
    assert "report_notes_count" not in out
    assert "report_collected" not in out
    # report_nm(메타)·다른 문서유형 필드는 보존
    assert out.get("report_nm") == "사업보고서"
    assert out.get("review_indexed") is True
    assert out.get("review_sep_indexed") is True


def test_strip_review_preserves_report():
    """review 삭제가 report_* 를 건드리지 않음(접두 독립성)."""
    entry = {"company": "신한", "period": "2025FY",
             "review_indexed": True, "report_indexed": True, "report_notes_count": 5}
    out = app._strip_doc_fields(entry, "review")
    assert "review_indexed" not in out
    assert out.get("report_indexed") is True
    assert out.get("report_notes_count") == 5


def test_doc_indexed_report():
    assert app._doc_indexed({"report_indexed": True}, "report") is True
    assert app._doc_indexed({"report_indexed": False}, "report") is False
    assert app._doc_indexed({"review_indexed": True}, "report") is False


def test_safe_original_filename_excludes_report():
    # report.pdf 작업본명은 original_ 접두로 회피
    assert app.safe_original_filename("report.pdf") == "original_report.pdf"
