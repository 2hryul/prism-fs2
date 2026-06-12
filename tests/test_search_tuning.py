"""검색개선 Phase1 단위테스트 — 혼합 유닛 스코어·BM25 제목/청크 분리·top-k 상수.

USE_BM25 OFF 로 cosine 경로를 결정론 통제(모델 불요). 임베딩은 2차원 단위벡터.
"""
import sys
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402

_Q = [1.0, 0.0]


def _unit(c):
    """cosine(_Q, v)=c 인 단위벡터."""
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


def _note(no, title, emb, chunks=None):
    return {"no": no, "title": title, "page_start": 10, "page_end": 12,
            "fs_div": "연결", "embedding": emb, "chunks": chunks or []}


def test_unit_cos_single_unit_equals_cosine():
    """유닛 1개(청크 없음) → 혼합점수 = cosine 그대로(구 인덱스 무회귀)."""
    score, page = app._note_unit_cos(_Q, _note(1, "t", _unit(0.8)))
    assert score == pytest.approx(0.8)
    assert page == 10


def test_unit_cos_breadth_bonus():
    """동일 max 라도 고르게 매칭된 노트(다중 청크)가 단발 스파이크보다 높은 점수."""
    spike = _note(1, "t", _unit(0.9), chunks=[
        {"text": "x", "page": 11, "embedding": _unit(0.1)},
        {"text": "x", "page": 12, "embedding": _unit(0.1)}])
    broad = _note(2, "t", _unit(0.9), chunks=[
        {"text": "x", "page": 11, "embedding": _unit(0.85)},
        {"text": "x", "page": 12, "embedding": _unit(0.85)}])
    s_spike, _ = app._note_unit_cos(_Q, spike)
    s_broad, _ = app._note_unit_cos(_Q, broad)
    assert s_broad > s_spike
    # max·상위3평균 혼합 공식 검증(가중치는 UNIT_MAX_W 상수 — 스윕으로 확정된 기본값)
    w = app.UNIT_MAX_W
    assert s_broad == pytest.approx(w * 0.9 + (1 - w) * (2.6 / 3))


def test_unit_cos_match_page_is_best_unit():
    """match_page 는 최고 유닛의 페이지(인용 정밀 유지)."""
    n = _note(1, "t", _unit(0.2), chunks=[
        {"text": "x", "page": 99, "embedding": _unit(0.95)}])
    score, page = app._note_unit_cos(_Q, n)
    assert page == 99


def test_bm25_title_signal_not_diluted(monkeypatch):
    """제목에만 질의어가 있는 노트가, 청크 토큰 바다에 묻히지 않고 우선돼야 한다."""
    if not app._HAS_BM25:
        pytest.skip("rank_bm25 미설치")
    monkeypatch.setattr(app, "USE_BM25", True)
    # 노트A: 제목이 질의어 정확 일치, 청크 없음. 노트B: 제목 무관, 청크에 질의어 1회 + 잡음 다수.
    # 필러 노트들로 코퍼스를 키워 IDF 가 유효한(0이 아닌) 조건에서 신호를 검증.
    noise = [{"text": "x", "page": 11, "embedding": _unit(0.0),
              "tokens": ["잡음"] * 50 + (["리스"] if i == 0 else [])} for i in range(5)]
    a = _note(1, "리스", _unit(0.0))
    a["tokens"] = ["리스"]
    b = _note(2, "무관제목", _unit(0.0), chunks=noise)
    b["tokens"] = ["무관", "제목"]
    fillers = []
    for i in range(3, 9):
        f = _note(i, f"기타주석{i}", _unit(0.0))
        f["tokens"] = [f"기타{i}", "주석"]
        fillers.append(f)
    res = app.rank_notes_for_query(_Q, "리스", [b, a] + fillers, k=8)
    assert res[0]["note_no"] == 1          # 제목 일치가 1위
    assert res[0]["score"] > res[1]["score"]  # 동점 타이브레이크가 아닌 점수 우위


def test_bm25_no_negative_explosion(monkeypatch):
    """전 문서 음수/0 BM25 점수에서도 정규화가 폭주하지 않는다(클립 후 0 유지)."""
    if not app._HAS_BM25:
        pytest.skip("rank_bm25 미설치")
    monkeypatch.setattr(app, "USE_BM25", True)
    a = _note(1, "리스", _unit(0.5)); a["tokens"] = ["리스"]
    b = _note(2, "무관", _unit(0.4)); b["tokens"] = ["무관"]
    cands = app._score_notes_for_query(_Q, "리스", [a, b])
    for c in cands:
        assert -1.0 <= c["score"] <= 1.5  # 점수 폭주(±수백만) 없음


def test_search_top_k_default():
    """Phase1: compare 후보 상한 기본 10."""
    assert app.SEARCH_TOP_K == 10
