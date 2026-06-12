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


def test_chunk_params_v3():
    """Phase2 파라미터 확정값 — 모델 128토큰 정합(250자)·상한 완화·스키마 v3."""
    assert app.CHUNK_CHARS == 250
    assert app.MAX_CHUNKS_PER_NOTE == 120
    assert app.INDEX_SCHEMA == 3
    assert not hasattr(app, "CHUNK_SCAN_PAGE_CAP")  # 페이지 스캔 캡 제거됨


def test_note_chunks_full_range_scan():
    """구 20페이지 캡 제거 — 30페이지 노트의 뒷부분(21p+)도 청크 생성."""
    pages = [f"{'가' * 300} 페이지{i + 1} 내용" for i in range(35)]
    doc = _FakeDoc(pages)
    chunks = app._note_chunks(doc, 1, 30)
    assert max(c["page"] for c in chunks) > 20  # 캡(20) 너머 스캔 확인
    assert len(chunks) <= app.MAX_CHUNKS_PER_NOTE


def test_chunk_text_size_250():
    """청크 길이 기본 250자(+오버랩 50) — 모델 절단 회피."""
    out = app._chunk_text("가" * 1000)
    assert all(len(c) <= 250 for c in out)
    # 오버랩: 두 번째 청크는 첫 청크 끝 50자를 공유
    assert out[1][:50] == out[0][-50:]


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