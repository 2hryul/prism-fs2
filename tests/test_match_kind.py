"""match_query_in_notes 의 note_kind 인지 채택 임계(B-2) 단위 테스트.

주기(정책 서술) 주석은 완화 하한(POLICY_MIN_MATCH=0.30), 서술형은 현행(MIN_MATCH_SCORE=0.35).
(검색개선 Phase1: 0.40/0.45 → 0.30/0.35 완화 — 재현율 우선, UI 는 confidence 로 구분.)
USE_BM25 를 끄면 final=cosine 이라 점수를 결정론으로 통제 가능(모델·BM25 불요).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def _note(no, title, emb):
    return {"no": no, "title": title, "page_start": 10, "page_end": 12,
            "fs_div": "연결", "embedding": emb, "chunks": []}


# q=[1,0] 와 cosine 0.32(= 0.30~0.35 사이) 인 단위벡터
_Q = [1.0, 0.0]
_EMB_032 = [0.32, 0.94742]


def test_policy_note_relaxed_threshold(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)  # cosine-only 경로 → score=cosine
    res = app.match_query_in_notes(_Q, "", [_note(1, "중요한 회계정책 및 작성기준", _EMB_032)])
    assert res["note_kind"] == "주기"
    assert res["min_match"] == 0.30
    assert 0.30 <= res["score"] < 0.35
    assert res["keep"] is True          # 0.32 >= 0.30(완화) → 채택


def test_narrative_note_keeps_strict_threshold(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)
    res = app.match_query_in_notes(_Q, "", [_note(2, "파생상품", _EMB_032)])
    assert res["note_kind"] == "서술형"
    assert res["min_match"] == 0.35
    assert res["keep"] is False         # 0.32 < 0.35(현행) → 동일 점수라도 미채택(무회귀)
