"""사업보고서(report) 본문 첨부 picker·fetch 오프라인 단위테스트(네트워크 없음).

검증 범위:
- pick_report_doc: 사업/분기/반기보고서 행 선택, 검토·감사보고서 배제
- 검토보고서만 있는 공시 → None(흔한 실제 케이스 — 본문은 첨부 PDF 아님)
- fetch_report_attachment → report.pdf, doc_type=report, source_type=full_report
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import collect_dart as cd  # noqa: E402


DOCS_WITH_REPORT = [
    {"title": "[신한지주]분기보고서(2025.11.14)", "url": "u_report"},
    {"title": "[신한지주]분기검토보고서(2025.11.14)", "url": "u_sep"},
    {"title": "[신한지주]분기연결검토보고서(2025.11.14)", "url": "u_conn"},
]
DOCS_REVIEW_ONLY = [
    {"title": "[KB금융]반기연결검토보고서(2025.08)", "url": "u_conn"},
    {"title": "[KB금융]감사보고서(2025.08)", "url": "u_audit"},
]


def test_pick_report_selects_body_report():
    cand = cd.pick_report_doc(DOCS_WITH_REPORT)
    assert cand is not None and cand["url"] == "u_report"


def test_pick_report_excludes_review_audit():
    # 검토/감사보고서만 있으면 본문 보고서 후보 없음 → None (업로드 폴백 케이스)
    assert cd.pick_report_doc(DOCS_REVIEW_ONLY) is None


def test_pick_report_requires_url():
    rows = [{"title": "[X]사업보고서", "url": ""}]
    assert cd.pick_report_doc(rows) is None


def test_pick_report_none_on_empty():
    assert cd.pick_report_doc([]) is None


class _FakeOdr:
    def __init__(self, files_by_url):
        self._files_by_url = files_by_url

    def attach_files(self, url):
        return self._files_by_url.get(url, {})


def test_fetch_report_attachment_writes_report_pdf(monkeypatch, tmp_path):
    def _fake_dl(url, dest_path):
        dest_path.write_bytes(b"%PDF-1.4 offline-fixture")
        return True
    monkeypatch.setattr(cd, "download_attachment", _fake_dl)
    odr = _FakeOdr({"u_report": {"[신한지주]분기보고서.pdf": "https://dart/pdf.do?report"}})
    res = cd.fetch_report_attachment(odr, "20250101", tmp_path,
                                     prefetched_docs=DOCS_WITH_REPORT)
    assert res is not None
    assert res["doc_type"] == "report"
    assert res["file"] == "report.pdf"
    assert res["source_type"] == "full_report"   # 사업보고서는 본문+주석 전체
    assert (tmp_path / "report.pdf").exists()


def test_fetch_report_none_when_no_body_report(monkeypatch, tmp_path):
    """검토보고서만 있는 공시 → 본문 미발견 → None(표준본 미생성)."""
    def _fake_dl(url, dest_path):
        dest_path.write_bytes(b"%PDF-1.4")
        return True
    monkeypatch.setattr(cd, "download_attachment", _fake_dl)
    odr = _FakeOdr({"u_conn": {"x.pdf": "https://dart/pdf.do?conn"}})
    res = cd.fetch_report_attachment(odr, "20250101", tmp_path,
                                     prefetched_docs=DOCS_REVIEW_ONLY)
    assert res is None
    assert not (tmp_path / "report.pdf").exists()
