"""사업보고서(FY/reprt_code 11011) FS-only 수집 단위테스트.

검증 범위(키·네트워크 불필요):
- 기간/윈도/보고서명 매핑이 FY 를 지원하는지 (순수 함수)
- collect_company(reprt_code=11011) 가 FS-only 로 동작 — document.xml/attach_docs 미호출,
  fs_structured.json 만 생성, meta.review_collected=False.
디스크는 tmp_path(LIBRARY_ROOT) 격리.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import collect_dart as cd  # noqa: E402


# ── 순수 함수 매핑 ────────────────────────────────────────────────────────────
def test_period_from_reprt_fy():
    assert cd.period_from_reprt(2025, "11011") == "2025FY"
    # 회귀: 분기 매핑 불변
    assert cd.period_from_reprt(2025, "11014") == "2025Q3"


def test_list_date_window_fy_next_year():
    # 사업보고서는 익년 1~6월 접수(3월경) → 윈도가 익년으로
    assert cd.list_date_window(2025, "11011") == ("20260101", "20260630")


def test_reprt_mark_fy():
    assert cd.REPRT_TO_PERIOD_MARK["11011"] == "12"  # 결산기 마커 (YYYY.12)


def test_pick_target_report_business_report():
    # report_nm 키워드 '사업보고서' + 마커 '2025.12' 동시 일치
    resp = {"status": "000", "list": [
        {"rcept_no": "RX", "report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260331"},
        {"rcept_no": "RY", "report_nm": "반기보고서 (2025.06)", "rcept_dt": "20250814"},
    ]}
    picked = cd.pick_target_report(resp, "11011", 2025)
    assert picked is not None and picked["rcept_no"] == "RX"


# ── FS-only 분기 (collect_company) ───────────────────────────────────────────
class _FakeResp:
    content = b""

    def json(self):
        # fnlttSinglAcntAll 정상 응답 1행(파서가 받아들이는 최소 형태)
        return {"status": "000", "message": "정상", "list": [
            {"sj_div": "BS", "sj_nm": "재무상태표", "fs_div": "CFS", "fs_nm": "연결재무제표",
             "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
             "thstrm_nm": "제18기", "thstrm_amount": "100", "frmtrm_nm": "제17기",
             "frmtrm_amount": "90", "ord": "1", "currency": "KRW"}]}


@pytest.fixture
def fy_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(cd, "_http_get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(cd, "pick_target_report", lambda *a, **k: {
        "rcept_no": "R1", "report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260331"})
    calls = {"unzip_to": 0, "attach_docs": 0, "review": 0, "review_sep": 0}
    # document.xml 해제(source/)·검토보고서 fetch 는 FY 에서 호출되면 안 됨
    monkeypatch.setattr(cd, "unzip_to",
                        lambda *a, **k: (calls.__setitem__("unzip_to", calls["unzip_to"] + 1), [])[1])
    monkeypatch.setattr(cd, "fetch_review_attachment",
                        lambda *a, **k: calls.__setitem__("review", calls["review"] + 1))
    monkeypatch.setattr(cd, "fetch_review_sep_attachment",
                        lambda *a, **k: calls.__setitem__("review_sep", calls["review_sep"] + 1))

    def _attach_docs(rcept_no):
        calls["attach_docs"] += 1
        return []
    odr = SimpleNamespace(attach_docs=_attach_docs)
    return tmp_path, calls, odr


def test_fy_is_fs_only(fy_env):
    """FY(11011): document.xml·attach_docs·검토보고서 fetch 전부 미호출, fs_structured.json 만 생성."""
    tmp_path, calls, odr = fy_env
    meta = cd.collect_company(None, "k", "신한", "C001", 2025, "11011", "2025FY", odr=odr)
    # FS-only — PDF/원문 경로 미진입
    assert calls["unzip_to"] == 0  # document.xml → source/ 스킵
    assert calls["attach_docs"] == 0
    assert calls["review"] == 0 and calls["review_sep"] == 0
    # 재무데이터는 수집됨
    fs = json.loads((tmp_path / "신한" / "2025FY" / "fs_structured.json").read_text(encoding="utf-8"))
    assert fs["by_fs_div"]["CFS"]["accounts"][0]["account_id"] == "ifrs-full_Assets"
    # meta 플래그: 재무데이터만, 검토보고서 없음
    assert meta["review_collected"] is False and meta["review_sep_collected"] is False
    assert meta["reprt_code"] == "11011"
    assert "CFS" in meta["fs_divs"]


def test_quarter_still_collects_docs(fy_env):
    """회귀: 분기(11014)는 종전대로 document.xml·attach_docs 진입."""
    tmp_path, calls, odr = fy_env
    cd.collect_company(None, "k", "신한", "C001", 2025, "11014", "2025Q3", odr=odr)
    assert calls["unzip_to"] >= 1  # document.xml 해제 시도
    assert calls["attach_docs"] == 1


# ── app 기간 검증/매핑 (FY 지원) ──────────────────────────────────────────────
def test_app_period_mapping_fy():
    import app  # noqa: E402 — 여러 테스트에서 이미 import 되는 모듈
    assert app.PERIOD_PATTERN.match("2025FY")
    assert not app.PERIOD_PATTERN.match("2025Q4")  # 회귀: Q4 거부
    assert app._period_to_year_reprt("2025FY") == (2025, "11011")
    # 정렬: 2025Q3 < 2025FY < 2026Q1
    src = ["2026Q1", "2025FY", "2025Q3"]
    assert sorted(src, key=app.period_sort_key) == ["2025Q3", "2025FY", "2026Q1"]
