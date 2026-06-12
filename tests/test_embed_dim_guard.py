"""임베딩 차원 불일치 가드 단위테스트.

검증 범위:
- _value_error_handler: 질의(512)↔인덱스(768) shape 불일치 ValueError → 503 + 원인·조치 안내
- _value_error_handler: 그 외 ValueError → 내부 구조 비노출 일반 500
- _sample_index_dim: 라이브러리 첫 인덱스의 임베딩 차원 반환(없으면 None)
네트워크·디스크 부작용 없음(handler 는 순수, dim 샘플은 tmp_path 격리).
"""
import sys
import json
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _body(resp):
    return json.loads(bytes(resp.body).decode("utf-8"))


# ── _value_error_handler ─────────────────────────────────────────────────────
def test_dim_mismatch_returns_503_with_guidance():
    """np.dot shape 불일치 메시지는 503 + 모델 로드/실행 안내로 변환."""
    exc = ValueError("shapes (512,) and (768,) not aligned: 512 (dim 0) != 768 (dim 0)")
    resp = _run(app._value_error_handler(None, exc))
    assert resp.status_code == 503
    detail = _body(resp)["detail"]
    assert "차원 불일치" in detail
    assert "ko-sroberta" in detail  # 조치 가이드 포함


def test_generic_value_error_hides_internals():
    """무관한 ValueError 는 내부 메시지 비노출 일반 500."""
    resp = _run(app._value_error_handler(None, ValueError("secret db path /etc/x")))
    assert resp.status_code == 500
    detail = _body(resp)["detail"]
    assert "secret" not in detail and "/etc/x" not in detail


# ── _sample_index_dim ────────────────────────────────────────────────────────
def test_sample_index_dim_reads_768(tmp_path, monkeypatch):
    lib = tmp_path / "library" / "신한" / "2025Q3"
    lib.mkdir(parents=True)
    idx = {"notes": [{"no": 1, "title": "현금", "embedding": [0.0] * 768}]}
    (lib / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    monkeypatch.setattr(app.paths, "LIBRARY_ROOT", tmp_path / "library")
    assert app._sample_index_dim() == 768


def test_sample_index_dim_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(app.paths, "LIBRARY_ROOT", tmp_path / "library")
    (tmp_path / "library").mkdir()
    assert app._sample_index_dim() is None
