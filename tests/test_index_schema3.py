"""검색개선 Phase2(스키마 v3) 단위테스트 — 청크 파라미터·전범위 스캔·반올림·전문 저장.

fitz 실 PDF 불요 — page_count/__getitem__ 만 흉내내는 가짜 doc 으로 결정론 검증.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


class _FakePage:
    def __init__(self, text):
        self._t = text

    def get_text(self):
        return self._t


class _FakeDoc:
    """페이지당 고정 텍스트를 주는 fitz.Document 대역."""
    def __init__(self, pages):
        self._pages = [_FakePage(t) for t in pages]
        self.page_count = len(pages)

    def __getitem__(self, i):
        return self._pages[i]


def test_chunk_params_v4():
    """파라미터 확정값 — 250자 한도·상한 완화·스키마 v4(문장경계 청크)."""
    assert app.CHUNK_CHARS == 250
    assert app.MAX_CHUNKS_PER_NOTE == 120
    assert app.INDEX_SCHEMA == 4
    assert not hasattr(app, "CHUNK_SCAN_PAGE_CAP")  # 페이지 스캔 캡 제거됨


def test_note_chunks_full_range_scan():
    """구 20페이지 캡 제거 — 30페이지 노트의 뒷부분(21p+)도 청크 생성."""
    pages = [f"{'가' * 300} 페이지{i + 1} 내용" for i in range(35)]
    doc = _FakeDoc(pages)
    chunks = app._note_chunks(doc, 1, 30)
    assert max(c["page"] for c in chunks) > 20  # 캡(20) 너머 스캔 확인
    assert len(chunks) <= app.MAX_CHUNKS_PER_NOTE


def test_chunk_text_long_sentence_fallback():
    """종결부호 없는 초장문(1문장) → 문자 슬라이싱 폴백. 각 청크 ≤250자, 50자 오버랩."""
    out = app._chunk_text("가" * 1000)
    assert all(len(c) <= 250 for c in out)
    assert out[1][:50] == out[0][-50:]  # 폴백 경로 오버랩 유지


def test_chunk_text_sentence_boundary():
    """다문장 입력은 문장 경계로 분할 — 문장 중간 절단 0, 각 청크 ≤250자."""
    sents = ["공정가치는 시장가격으로 측정한다.",
             "수준3 자산은 평가기법을 사용한다.",
             "민감도 분석은 할인율 가정에 기반한다."]
    out = app._chunk_text(" ".join(sents))
    assert all(len(c) <= 250 for c in out)
    # 모든 청크는 온전한 문장(들)로 끝남 — 중간 절단 없음
    for c in out:
        assert c.rstrip().endswith(".")
    # 짧은 문장들은 한 청크로 묶임(합계 ≤250)
    assert len(out) == 1


def test_chunk_text_packs_until_limit():
    """문장 누적이 250자 한도를 넘기 직전 청크 확정 — 다음 문장은 새 청크."""
    s = ("공정가치 측정과 평가 가정을 설명하는 회계 주석 본문 예시 문장으로 "
         "약 백자 내외 길이를 갖도록 충분히 길게 작성된 테스트 문장이다.")  # ~70자
    assert len(s) <= 250  # 단일 문장은 한도 이내(폴백 아님)
    out = app._chunk_text(" ".join([s, s, s, s]))  # 4문장 ≈ 280자+ → 2청크 이상
    assert len(out) >= 2
    assert all(len(c) <= 250 for c in out)
    for c in out:  # 문장 경계 유지(중간 절단 0)
        assert c.rstrip().endswith(".")


def test_round_emb():
    """저장 벡터 반올림 — 소수점 5자리 이하."""
    out = app._round_emb([0.123456789, -0.000012345, 1.0])
    assert out == [0.12346, -1e-05, 1.0]
    for x in out:
        assert len(str(x).split(".")[-1].replace("e-05", "")) <= 7


def test_note_full_text_cap_and_join():
    """노트 전문 — 페이지 범위 결합·공백 정규화·상한 방어."""
    doc = _FakeDoc(["첫  페이지\t내용", "둘째 페이지", "범위밖"])
    txt = app._note_full_text(doc, 1, 2)
    assert "첫 페이지 내용" in txt and "둘째 페이지" in txt
    assert "범위밖" not in txt
    # 상한 방어
    big = _FakeDoc(["가" * 100_000] * 5)
    assert len(app._note_full_text(big, 1, 5)) <= app._FULL_TEXT_CAP


def test_make_embeddings_batch_matches_single(monkeypatch):
    """배치 임베딩 == 단건 임베딩(동일 백엔드) — bigram 폴백 경로로 결정론 검증."""
    import asyncio
    import numpy as np
    monkeypatch.setattr(app, "USE_LOCAL_EMBED", False)
    texts = ["리스 회계처리", "충당부채"]
    batch = asyncio.run(app.make_embeddings(texts))
    for i, t in enumerate(texts):
        single = asyncio.run(app.make_embedding(t))
        assert np.allclose(batch[i], single)