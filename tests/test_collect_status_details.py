"""DART 수집 상태 상세(_doc_detail/_collect_details) 단위테스트.

검증 범위:
- _doc_detail: 파일명 변경(renamed) 판정, manual_upload_required, 무클로버 재수집 시
  디스크 기준 collected + original_pdf_name 폴백, INDEX_STATUS 연동(notes_count)
- _collect_details: documents/fs_divs/path 계약 — meta 부재 시 빈 값 폴백
디스크는 tmp_path 로 격리(app.LIBRARY_ROOT/INDEX_STATUS 몽키패치), 네트워크 없음.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """app 의 라이브러리 루트·인덱싱 상태를 tmp 로 격리."""
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(app, "LIBRARY_ROOT", root)
    monkeypatch.setattr(app, "CATALOG_PATH", tmp_path / "catalog.json")
    monkeypatch.setattr(app, "INDEX_STATUS", {})
    return root


def _cell(root, company="신한", period="2025Q3"):
    d = root / company / period
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_doc_detail_renamed(lib):
    """표준명으로 저장(원본명≠저장명)된 review → renamed=True + 인덱싱 수치 전달."""
    d = _cell(lib)
    (d / "review.pdf").write_bytes(b"%PDF-1.4")
    app.INDEX_STATUS["신한/2025Q3/review"] = {
        "status": "done", "notes_extracted": 30, "detected_unit": "억원"}
    out = app._doc_detail("신한", "2025Q3", {
        "doc_type": "review", "file": "review.pdf",
        "filename_original": "분기연결검토보고서.pdf",
        "filename_dart": "분기연결검토보고서",
    })
    assert out["renamed"] is True
    assert out["collected"] is True
    assert out["file"] == "review.pdf"
    assert out["filename_original"] == "분기연결검토보고서.pdf"
    assert out["indexed"] is True and out["notes_count"] == 30
    assert out["detected_unit"] == "억원"


def test_doc_detail_manual_upload(lib):
    """본문 PDF 미제공(report) → manual_upload_required=True, 미수집·미변경."""
    _cell(lib)
    out = app._doc_detail("신한", "2025Q3", {
        "doc_type": "report", "display_pdf": "manual_upload_required"})
    assert out["manual_upload_required"] is True
    assert out["collected"] is False
    assert out["renamed"] is False
    assert out["indexed"] is False and out["notes_count"] is None


def test_doc_detail_noclobber_report(lib):
    """무클로버 재수집(meta 에 file 없음)이라도 디스크 PDF 기준 collected=True,
    저장명은 표준 작업본 폴백, 원본명은 디렉터리 스캔 폴백(original_pdf_name)."""
    d = _cell(lib)
    (d / "report.pdf").write_bytes(b"%PDF-1.4")
    (d / "[신한지주]분기보고서(2025.11.14).pdf").write_bytes(b"%PDF-1.4")
    out = app._doc_detail("신한", "2025Q3",
                          {"doc_type": "report", "display_pdf": "manual_upload_required"})
    assert out["collected"] is True
    assert out["file"] == "report.pdf"  # 표준 작업본 폴백
    assert out["filename_original"] == "[신한지주]분기보고서(2025.11.14).pdf"
    assert out["renamed"] is True  # 원본명 → 표준명 저장
    assert out["manual_upload_required"] is False  # 디스크에 본문 존재 → 안내 불필요


def test_collect_details_synthesizes_disk_docs(lib):
    """무클로버 스킵으로 meta.documents 에 없는 문서도 디스크 작업본 기준으로 합성."""
    d = _cell(lib)
    (d / "review.pdf").write_bytes(b"%PDF-1.4")
    app.INDEX_STATUS["신한/2025Q3/review"] = {"status": "done", "notes_extracted": 28}
    out = app._collect_details("신한", "2025Q3", {"documents": []})
    assert [x["doc_type"] for x in out["documents"]] == ["review"]
    doc = out["documents"][0]
    assert doc["collected"] is True and doc["file"] == "review.pdf"
    assert doc["indexed"] is True and doc["notes_count"] == 28


def test_collect_details_empty_meta(lib):
    """documents/fs_divs 키 없음 → 빈 값 폴백 + 저장경로는 entry_dir 절대경로."""
    _cell(lib)
    out = app._collect_details("신한", "2025Q3", {})
    assert out["documents"] == []
    assert out["fs_divs"] == []
    assert out["path"] == str(app.entry_dir("신한", "2025Q3"))


def test_collect_details_fs_divs(lib):
    """fs_divs(연결 CFS 만 수집)와 documents 패스스루."""
    d = _cell(lib)
    (d / "review_sep.pdf").write_bytes(b"%PDF-1.4")
    out = app._collect_details("신한", "2025Q3", {
        "fs_divs": ["CFS"],
        "documents": [{"doc_type": "review_sep", "file": "review_sep.pdf",
                       "filename_original": "분기검토보고서.pdf"}],
    })
    assert out["fs_divs"] == ["CFS"]
    assert len(out["documents"]) == 1
    doc = out["documents"][0]
    assert doc["doc_type"] == "review_sep" and doc["collected"] is True
    assert doc["renamed"] is True
    assert doc["existing"] is False  # 이번 런 fetch 결과(meta 에 file 존재) → 신규


def test_collect_details_fetch_failures_passthrough(lib):
    """meta.fetch_failures({reason, url}) → 상태 상세에 그대로 전파(없으면 빈 dict)."""
    _cell(lib)
    out = app._collect_details("신한", "2025Q3", {
        "fetch_failures": {"review_sep": {
            "reason": "PDF 다운로드 실패(DART 응답 없음 — 재시도 권장)",
            "url": "https://dart.fss.or.kr/pdf/download/x.pdf"}}})
    fail = out["fetch_failures"]["review_sep"]
    assert fail["reason"].startswith("PDF 다운로드 실패")
    assert fail["url"].startswith("https://dart.fss.or.kr/")
    assert app._collect_details("신한", "2025Q3", {})["fetch_failures"] == {}


def test_doc_detail_existing_flags(lib):
    """이번 런 미수집·디스크 보유 → existing=True (합성 항목·무클로버 meta 항목 모두)."""
    d = _cell(lib)
    (d / "report.pdf").write_bytes(b"%PDF-1.4")
    # 무클로버: meta 의 report 항목엔 fetch 결과 필드(file/filename_dart) 없음
    noclobber = app._doc_detail("신한", "2025Q3", {"doc_type": "report"})
    assert noclobber["existing"] is True
    # 합성 항목(meta documents 에 아예 없던 디스크 파일)도 동일 경로 → existing=True
    synth = app._collect_details("신한", "2025Q3", {"documents": []})["documents"][0]
    assert synth["doc_type"] == "report" and synth["existing"] is True
    # 이번 런 fetch 성공 항목 → existing=False
    fetched = app._doc_detail("신한", "2025Q3", {
        "doc_type": "report", "file": "report.pdf", "filename_dart": "분기보고서.pdf"})
    assert fetched["existing"] is False
