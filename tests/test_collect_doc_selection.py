"""DART 수집 문서 선택(collect_review/collect_review_sep) 게이팅 단위테스트.

검증 범위:
- collect_company 가 선택된 검토보고서만 fetch 하는지 (연결/별도 독립 토글)
- 둘 다 미선택이면 attach_docs 호출 자체를 생략하는지
네트워크 없음: _http_get/unzip_to/pick_target_report/fetch_* 전부 monkeypatch,
디스크는 tmp_path(LIBRARY_ROOT) 격리.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import collect_dart as cd  # noqa: E402


class _FakeResp:
    """_http_get 대역 — .json()/.content 만 흉내."""
    content = b""

    def json(self):
        return {"status": "013", "message": "no data", "list": []}


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """collect_company 의 외부 의존 전부 차단 + fetch 호출 기록기 반환."""
    monkeypatch.setattr(cd, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(cd, "_http_get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(cd, "unzip_to", lambda *a, **k: [])
    monkeypatch.setattr(cd, "pick_target_report", lambda *a, **k: {
        "rcept_no": "R1", "report_nm": "분기보고서 (2025.09)", "rcept_dt": "20251114"})
    calls = {"attach_docs": 0, "review": 0, "review_sep": 0}
    monkeypatch.setattr(cd, "fetch_review_attachment",
                        lambda *a, **k: calls.__setitem__("review", calls["review"] + 1))
    monkeypatch.setattr(cd, "fetch_review_sep_attachment",
                        lambda *a, **k: calls.__setitem__("review_sep", calls["review_sep"] + 1))

    def _attach_docs(rcept_no):
        calls["attach_docs"] += 1
        return []
    odr = SimpleNamespace(attach_docs=_attach_docs)
    return calls, odr


def _collect(odr, **sel):
    return cd.collect_company(None, "k", "신한", "C001", 2025, "11014", "2025Q3",
                              odr=odr, **sel)


def test_review_only(gated):
    """연결만 선택 → 별도 fetch 미호출."""
    calls, odr = gated
    _collect(odr, collect_review=True, collect_review_sep=False)
    assert calls["review"] == 1 and calls["review_sep"] == 0


def test_review_sep_only(gated):
    """별도만 선택 → 연결 fetch 미호출."""
    calls, odr = gated
    _collect(odr, collect_review=False, collect_review_sep=True)
    assert calls["review"] == 0 and calls["review_sep"] == 1


def test_none_selected_skips_attach_docs(gated):
    """둘 다 미선택 → attach_docs 네트워크 호출 자체 생략."""
    calls, odr = gated
    meta = _collect(odr, collect_review=False, collect_review_sep=False)
    assert calls["attach_docs"] == 0
    assert calls["review"] == 0 and calls["review_sep"] == 0
    assert meta["review_collected"] is False and meta["review_sep_collected"] is False


def test_default_collects_both(gated):
    """기본값(미지정) → 양쪽 모두 수집 시도(기존 동작 보존)."""
    calls, odr = gated
    _collect(odr)
    assert calls["attach_docs"] == 1
    assert calls["review"] == 1 and calls["review_sep"] == 1


# ── 실패 사유 전파(fetch_failures) ───────────────────────────────────────────
def test_fetch_failure_reason_recorded(tmp_path, monkeypatch):
    """다운로드 실패 시 fail_reasons 에 doc_type→사유 기록 + None 반환(계약 불변)."""
    monkeypatch.setattr(cd, "download_attachment", lambda *a, **k: False)
    odr = SimpleNamespace(
        attach_docs=lambda r: [{"title": "2025.11.14 분기검토보고서", "url": "u_sep"}],
        attach_files=lambda u: {"분기검토보고서.pdf": "pdf_u"},
    )
    reasons = {}
    out = cd._fetch_attachment_pdf(odr, "R1", tmp_path,
                                   picker=cd.pick_review_doc_separate,
                                   out_name="review_sep.pdf", doc_type="review_sep",
                                   fail_reasons=reasons)
    assert out is None
    assert "다운로드 실패" in reasons["review_sep"]["reason"]
    # 사용자 직접 다운로드용 — 실패한 첨부의 실제 PDF URL 을 그대로 전달
    assert reasons["review_sep"]["url"] == "pdf_u"


def test_fetch_no_candidate_reason(tmp_path):
    """후보 없음(공시에 해당 첨부 미제공) → 사유 기록."""
    odr = SimpleNamespace(attach_docs=lambda r: [], attach_files=lambda u: {})
    reasons = {}
    out = cd._fetch_attachment_pdf(odr, "R1", tmp_path,
                                   picker=cd.pick_review_doc_separate,
                                   out_name="review_sep.pdf", doc_type="review_sep",
                                   prefetched_docs=[], fail_reasons=reasons)
    assert out is None
    assert "공시에 없음" in reasons["review_sep"]["reason"]
    # 직접 받을 첨부가 없으므로 공시 뷰어 링크로 폴백
    assert "rcptNo=R1" in reasons["review_sep"]["url"]


def test_collect_company_meta_fetch_failures(gated, monkeypatch):
    """collect_company: 선택 문서 실패 사유가 meta.fetch_failures 로 전파."""
    calls, odr = gated
    monkeypatch.setattr(cd, "fetch_review_sep_attachment",
                        lambda *a, **k: k["fail_reasons"].__setitem__(
                            "review_sep", {"reason": "PDF 다운로드 실패(테스트)", "url": "u"}))
    meta = _collect(odr, collect_review=False, collect_review_sep=True)
    assert meta["fetch_failures"] == {"review_sep": {"reason": "PDF 다운로드 실패(테스트)",
                                                     "url": "u"}}


def test_collect_company_no_failures_empty(gated):
    """실패 없으면 fetch_failures 는 빈 dict (미선택 문서는 미기록)."""
    calls, odr = gated
    meta = _collect(odr, collect_review=False, collect_review_sep=False)
    assert meta["fetch_failures"] == {}


# ── pdf.do Referer 도출 (다운로드 빈 응답 수정) ──────────────────────────────
def test_dart_download_referer_from_pdf_url():
    """pdf.do URL 쿼리(rcp_no/dcm_no)로 문서별 다운로드 페이지 Referer 구성."""
    url = "http://dart.fss.or.kr/pdf/download/pdf.do?rcp_no=20251114002208&dcm_no=10882571"
    ref = cd._dart_download_referer(url)
    assert ref == ("https://dart.fss.or.kr/pdf/download/main.do"
                   "?rcp_no=20251114002208&dcm_no=10882571")


def test_dart_download_referer_fallback():
    """rcp_no/dcm_no 없는 URL → 호스트 루트 폴백."""
    assert cd._dart_download_referer("https://dart.fss.or.kr/x.pdf") == "https://dart.fss.or.kr/"
