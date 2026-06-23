"""회계기준서(accounting standards) 백엔드 단위테스트 — doc_id·카탈로그·인덱싱·검색 라우팅.

전역 STANDARDS_ROOT/CATALOG 를 tmp 로 격리(실 storage 무오염). 임베딩은 더미로 대체해 빠르게.
"""
import sys
import json
import asyncio
from pathlib import Path

import pytest
import fitz  # PyMuPDF — 테스트용 소형 PDF 생성

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402
import notes_rag  # noqa: E402


@pytest.fixture
def std_env(tmp_path, monkeypatch):
    sroot = tmp_path / "accounting_standards"
    sroot.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app, "STANDARDS_ROOT", sroot)
    monkeypatch.setattr(app, "STANDARDS_CATALOG_PATH", sroot / "catalog.json")
    return sroot


def test_doc_id_idempotent_and_safe(std_env):
    a = app._standard_doc_id("K-IFRS 1109 금융상품.pdf", b"hello")
    b = app._standard_doc_id("K-IFRS 1109 금융상품.pdf", b"hello")
    c = app._standard_doc_id("K-IFRS 1109 금융상품.pdf", b"different")
    assert a == b            # 동일 내용 → 멱등
    assert a != c            # 동명이내용 → 분리
    for ch in ("/", "\\", ".."):
        assert ch not in a
    # traversal 파일명도 슬러그에서 제거
    slug = app._slugify_standard("../../etc/pass wd.pdf")
    assert "/" not in slug and ".." not in slug and slug == "pass_wd"


def test_catalog_upsert_remove_isolated(std_env):
    # 기존 라이브러리 카탈로그와 별개 경로
    assert app.CATALOG_PATH != app.STANDARDS_CATALOG_PATH
    app.upsert_standard("d1", title="A", indexed=False)
    app.upsert_standard("d1", indexed=True, chunks=5)   # 머지(기존 title 보존)
    d = next(x for x in app.load_standards_catalog()["docs"] if x["doc_id"] == "d1")
    assert d["title"] == "A" and d["indexed"] is True and d["chunks"] == 5
    app.upsert_standard("d2", title="B")
    app.remove_standard("d1")
    ids = [x["doc_id"] for x in app.load_standards_catalog()["docs"]]
    assert ids == ["d2"]
    assert app._standard_exists("d2") and not app._standard_exists("d1")


def test_standard_paths(std_env):
    assert app.standard_pdf_path("xyz").name == "doc.pdf"
    assert app.standard_index_path("xyz").name == "index_body.json"
    assert app.standard_dir("xyz").parent == std_env


def test_index_standard_builds_body_index(std_env, monkeypatch):
    doc_id = "test_doc_aaaa1111"
    d = std_env / doc_id
    d.mkdir(parents=True)
    pdf = fitz.open()
    for txt in ["리스부채는 최초에 현재가치로 측정한다. " * 6,
                "금융자산의 손상은 기대신용손실로 인식한다. " * 6]:
        page = pdf.new_page()
        page.insert_text((72, 72), txt)
    pdf.save(str(d / "doc.pdf"))
    pdf.close()
    app.upsert_standard(doc_id, title="T", indexed=False, pages=2)

    async def fake_embs(texts):
        import numpy as np
        return [np.array([1.0, 0.0]) for _ in texts]
    monkeypatch.setattr(app, "make_embeddings", fake_embs)

    asyncio.run(app._index_standard(doc_id))

    ipath = std_env / doc_id / "index_body.json"
    assert ipath.exists()
    idx = json.loads(ipath.read_text(encoding="utf-8"))
    assert idx["default_fs_div"] == "all"
    assert idx["notes"], "본문 유닛이 비어있음"
    assert all(n["no"].startswith("B") for n in idx["notes"])   # 본문 유닛
    assert all(n["fs_div"] == "all" for n in idx["notes"])      # fs_div 중립
    st = app.INDEX_STATUS.get(f"standards/{doc_id}")
    assert st and st["status"] == "done" and st["chunks"] > 0
    # 카탈로그 인덱싱 반영
    doc = next(x for x in app.load_standards_catalog()["docs"] if x["doc_id"] == doc_id)
    assert doc["indexed"] is True and doc["chunks"] > 0


def test_search_routing_returns_doc_id():
    """standards 인덱스를 cell 로 주입한 retrieve 가 doc_id 를 company 로 반환."""
    idx = {"notes": [{
        "no": "B1", "title": "본문 p.1", "fs_div": "all",
        "page_start": 1, "page_end": 1, "embedding": [1.0, 0.0],
        "chunks": [{"text": "리스부채 현재가치 측정", "page": 1,
                    "embedding": [1.0, 0.0], "tokens": ["리스부채"]}],
    }]}
    cells = [{"company": "doc_xyz", "period": "-", "index": idx}]
    got = notes_rag.retrieve([1.0, 0.0], cells, fs_div="all", top_k=5)
    assert got and got[0]["company"] == "doc_xyz"
    assert got[0]["note_no"] == "B1"


def _make_pdf(path, pages_lines):
    """각 줄을 별도 줄로 갖는 PDF 생성(테스트용). pages_lines: [[line,...], ...]."""
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page()
        y = 72
        for ln in lines:
            page.insert_text((72, y), ln, fontsize=11)
            y += 22
    doc.save(str(path))
    doc.close()


def test_segment_standard_lines():
    """문단 분할 코어 — 섹션 태깅·표숫자 오탐 차단·구획(BC) 인식.

    (한글은 fitz 기본 폰트로 PDF 추출이 안 되므로 라인 코어를 직접 검증. 실제 PDF 경로는
    extract_standard_paragraphs 가 동일 코어를 호출.)
    """
    pages = [
        (1, ["목  차", "목적", "적용범위", "위험"]),
        (2, ["목적", "1", "이 기준서의 목적은 금융상품 공시 사항을 정하는 것이다.",
             "2", "이 기준서의 원칙은 표시와 인식 측정을 보완하는 것이다 충분히 길게.",
             "적용범위", "3", "이 기준서는 모든 유형의 금융상품에 적용한다 충분히 길게.",
             "900", "이것은 표 안 숫자처럼 큰 점프라 본문 앵커가 아니어야 한다.",
             "위험", "BC1", "결론도출근거 문단으로 위험 관련 배경 설명 충분히 길게."]),
    ]
    paras = app._segment_standard_lines(pages)
    by_no = {p["no"]: p for p in paras}
    assert set(["1", "2", "3", "BC1"]).issubset(by_no.keys())
    assert "900" not in by_no                       # 단조증가 가드로 표숫자 배제
    assert by_no["1"]["section"] == "목적"
    assert by_no["3"]["section"] == "적용범위"
    assert by_no["1"]["part"] == "본문"
    assert by_no["BC1"]["part"] == "결론도출근거"
    assert by_no["1"]["page_start"] == 2


def test_build_standard_index_fallback(tmp_path, monkeypatch, std_env):
    """문단 앵커가 거의 없는 비정형 PDF → 페이지 단위 폴백(structured=False)."""
    doc_id = "plain_doc_bbbb2222"
    d = std_env / doc_id
    d.mkdir(parents=True)
    _make_pdf(d / "doc.pdf", [
        ["회계 일반 산문 문서입니다. 문단번호가 없는 비정형 텍스트 본문." * 2],
        ["두 번째 페이지도 비정형 산문 텍스트로만 구성되어 있습니다." * 2],
    ])

    async def fake_embs(texts):
        import numpy as np
        return [np.array([1.0, 0.0]) for _ in texts]
    monkeypatch.setattr(app, "make_embeddings", fake_embs)

    res = asyncio.run(app.build_standard_index(doc_id, d / "doc.pdf",
                                               app.standard_index_path(doc_id)))
    assert res["structured"] is False              # 폴백
    idx = json.loads((d / "index_body.json").read_text(encoding="utf-8"))
    assert idx["notes"] and all(n["no"].startswith("B") for n in idx["notes"])
