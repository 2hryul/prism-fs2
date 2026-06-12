"""LLM 후보 재정렬(llm_rerank_candidates, 검색개선 Phase4) 단위테스트.

Ollama 호출은 httpx.AsyncClient 대역으로 결정론 통제 — 네트워크 없음.
"""
import sys
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def _cands():
    return [{"note_no": 1, "title": "사채"},
            {"note_no": 2, "title": "확정급여제도 자산 및 부채"},
            {"note_no": 9, "title": "충당부채"}]


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _fake_client(response_text):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _FakeResp({"response": response_text})
    return _FakeClient


def test_rerank_reorders_and_preserves_unlisted(monkeypatch):
    """LLM 이 [2,9] 반환 → 2,9 우선 + 미언급 1은 뒤에 보존(누락 금지)."""
    monkeypatch.setattr(app, "_OLLAMA_AVAILABLE", True)
    monkeypatch.setattr(app.httpx, "AsyncClient", _fake_client("[2, 9]"))
    out = asyncio.run(app.llm_rerank_candidates("퇴직급여", _cands()))
    assert [c["note_no"] for c in out] == [2, 9, 1]
    assert all(c.get("reranked") for c in out)  # AI 개입 투명화 플래그


def test_rerank_ignores_hallucinated_nos(monkeypatch):
    """후보에 없는 no(99)는 무시 — 할루시네이션 항목 추가 불가."""
    monkeypatch.setattr(app, "_OLLAMA_AVAILABLE", True)
    monkeypatch.setattr(app.httpx, "AsyncClient", _fake_client("[99, 9]"))
    out = asyncio.run(app.llm_rerank_candidates("충당부채", _cands()))
    assert [c["note_no"] for c in out] == [9, 1, 2]
    assert len(out) == 3


def test_rerank_garbage_response_keeps_order(monkeypatch):
    """JSON 배열이 아니면 원본 순서 그대로(무회귀)."""
    monkeypatch.setattr(app, "_OLLAMA_AVAILABLE", True)
    monkeypatch.setattr(app.httpx, "AsyncClient", _fake_client("설명: 재정렬 불가"))
    cands = _cands()
    out = asyncio.run(app.llm_rerank_candidates("질의", cands))
    assert [c["note_no"] for c in out] == [1, 2, 9]
    assert not any(c.get("reranked") for c in out)


def test_rerank_unavailable_passthrough(monkeypatch):
    """Ollama 비가용 → 호출 없이 원본 그대로."""
    monkeypatch.setattr(app, "_OLLAMA_AVAILABLE", False)
    cands = _cands()
    assert asyncio.run(app.llm_rerank_candidates("질의", cands)) is cands