# -*- coding: utf-8 -*-
"""검색 품질 평가 하네스 — 골든 질의셋 기반 Recall@5/10, MRR, Visible@5 산출.

실행(프로젝트 루트):
    python src\\eval_search.py [태그]
- 앱과 동일한 스코어링(app.rank_notes_for_query)을 그대로 사용해 실측한다.
- 평가 단위: (질의 × 인덱스 코퍼스). 코퍼스 = index*.json × fs_div(연결/별도).
  기대 주석(제목 부분문자열 매칭)이 없는 코퍼스는 해당 질의에서 제외(스킵).
- Visible@5: 상위5 안에 기대 주석이 있고 keep=True(임계 통과 — UI 실제 노출 기준).
- 결과는 doc\\eval\\eval_{태그}.json 저장 — 개선 전후 비교용.
"""
import sys
import io
import json
import asyncio
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402  (임베딩 모델 로드 포함 — 1회)

GOLDEN = ROOT / "tests" / "golden_queries.json"
OUT_DIR = ROOT / "doc" / "eval"


def _norm(s: str) -> str:
    """제목 매칭용 정규화 — 공백 제거(회사별 표기 변형 흡수)."""
    return (s or "").replace(" ", "")


_STEM_TO_DOC = {"index": "report", "index_review": "review",
                "index_review_sep": "review_sep"}
_DOC_TO_PDF = {"report": "report.pdf", "review": "review.pdf",
               "review_sep": "review_sep.pdf"}


def _iter_corpora():
    """라이브러리의 평가 코퍼스 (key, notes, pdf_path) 순회.

    백업본(index_heuristic.bak)은 제외. fs_div 축이 없는 구 인덱스는 단일 코퍼스.
    """
    for idx_file in sorted(app.LIBRARY_ROOT.glob("*/*/index*.json")):
        if "heuristic" in idx_file.name:
            continue
        cell = f"{idx_file.parent.parent.name}/{idx_file.parent.name}"
        data = json.loads(idx_file.read_text(encoding="utf-8"))
        notes = [n for n in data.get("notes", []) if "embedding" in n]
        if not notes:
            continue
        doc_type = _STEM_TO_DOC.get(idx_file.stem, idx_file.stem)
        pdf = idx_file.parent / _DOC_TO_PDF.get(doc_type, "")
        fs_divs = sorted({n.get("fs_div") for n in notes if n.get("fs_div")}) or [None]
        for fs in fs_divs:
            sub = [n for n in notes if fs is None or n.get("fs_div") == fs]
            if sub:
                yield f"{cell}/{doc_type}/{fs or '단일'}", sub, pdf


def _pdf_pages_text(pdf_path: Path) -> list:
    """PDF 전 페이지 텍스트(0-base 리스트). 파라미터 스윕용 디스크 캐시(mtime 키)."""
    import hashlib
    cache_dir = OUT_DIR / ".pdfcache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{pdf_path}|{pdf_path.stat().st_mtime_ns}".encode()).hexdigest()
    cf = cache_dir / f"{key}.json"
    if cf.exists():
        return json.loads(cf.read_text(encoding="utf-8"))
    import fitz
    with fitz.open(pdf_path) as doc:
        pages = [doc[i].get_text() for i in range(doc.page_count)]
    cf.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return pages


def _expected_by_body(notes, pages_text, phrases) -> set:
    """본문 근거 정답: 노트 페이지 범위(PDF 원문)에 구절이 실제 존재하는 노트 번호 집합.

    인덱스(샘플링된 청크)가 아니라 PDF 원문 기준 — 청크 누락·잘림 손실을 그대로 측정.
    """
    out = set()
    for n in notes:
        ps, pe = n.get("page_start"), n.get("page_end") or n.get("page_start")
        if not ps:
            continue
        body = "\n".join(pages_text[ps - 1:min(pe, len(pages_text))])
        if any(p in body for p in phrases):
            out.add(n["no"])
    return out


async def run(tag: str):
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    # (레벨, 질의) — title: 제목 라벨(쉬움), content: PDF 본문 근거 라벨(샘플링 손실 측정)
    all_qs = ([("title", q) for q in g["queries"]]
              + [("content", q) for q in g.get("content_queries", [])])
    q_embs = {q["id"]: await app.make_embedding(q["query"]) for _, q in all_qs}

    per_q = {q["id"]: {"query": q["query"], "level": lvl, "pairs": 0,
                       "hit5": 0, "hit10": 0, "vis5": 0, "rr_sum": 0.0}
             for lvl, q in all_qs}
    pdf_cache: dict = {}

    for corpus_key, notes, pdf in _iter_corpora():
        pages = None
        for lvl, q in all_qs:
            if lvl == "title":
                expects = [_norm(e) for e in q["expect_title_any"]]
                expected_nos = {n["no"] for n in notes
                                if any(e in _norm(n.get("title", "")) for e in expects)}
            else:
                if not pdf.exists():
                    continue
                if pages is None:
                    if pdf not in pdf_cache:
                        pdf_cache[pdf] = _pdf_pages_text(pdf)
                    pages = pdf_cache[pdf]
                expected_nos = _expected_by_body(notes, pages, q["expect_body_any"])
            if not expected_nos:
                continue  # 이 코퍼스엔 기대 주석 없음 → 평가 제외
            ranked = app.rank_notes_for_query(q_embs[q["id"]], q["query"], notes,
                                              k=len(notes))
            rank = next((i + 1 for i, c in enumerate(ranked)
                         if c["note_no"] in expected_nos), None)
            st = per_q[q["id"]]
            st["pairs"] += 1
            if rank is not None:
                st["rr_sum"] += 1.0 / rank
                if rank <= 5:
                    st["hit5"] += 1
                    if any(c["note_no"] in expected_nos and c["keep"]
                           for c in ranked[:5]):
                        st["vis5"] += 1
                if rank <= 10:
                    st["hit10"] += 1

    rows = []
    levels = {"title": dict(pairs=0, hit5=0, hit10=0, vis5=0, rr_sum=0.0),
              "content": dict(pairs=0, hit5=0, hit10=0, vis5=0, rr_sum=0.0)}
    for qid, st in sorted(per_q.items()):
        if st["pairs"] == 0:
            continue
        agg = levels[st["level"]]
        for k in ("pairs", "hit5", "hit10", "vis5", "rr_sum"):
            agg[k] += st[k]
        rows.append({"id": qid, "level": st["level"], "query": st["query"],
                     "pairs": st["pairs"],
                     "recall@5": round(st["hit5"] / st["pairs"], 3),
                     "recall@10": round(st["hit10"] / st["pairs"], 3),
                     "visible@5": round(st["vis5"] / st["pairs"], 3),
                     "mrr": round(st["rr_sum"] / st["pairs"], 3)})

    def _summary(agg):
        if agg["pairs"] == 0:
            return None
        return {"pairs": agg["pairs"],
                "recall@5": round(agg["hit5"] / agg["pairs"], 4),
                "recall@10": round(agg["hit10"] / agg["pairs"], 4),
                "visible@5": round(agg["vis5"] / agg["pairs"], 4),
                "mrr": round(agg["rr_sum"] / agg["pairs"], 4)}

    summary = {
        "tag": tag,
        "title_level": _summary(levels["title"]),
        "content_level": _summary(levels["content"]),
        "config": {"MIN_MATCH_SCORE": app.MIN_MATCH_SCORE,
                   "backend": "local" if app.USE_LOCAL_EMBED else "bigram"},
        "per_query": rows,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"eval_{tag}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n=== 검색 품질 평가 [{tag}] ===")
    for lvl, label in (("title", "제목 수준(쉬움)"), ("content", "본문 수준(어려움)")):
        s = summary[f"{lvl}_level"]
        if not s:
            continue
        print(f"[{label}] pairs={s['pairs']}  R@5={s['recall@5']:.1%}  "
              f"R@10={s['recall@10']:.1%}  Vis@5={s['visible@5']:.1%}  MRR={s['mrr']:.3f}")
    print(f"→ 저장: {out}")
    worst = sorted(rows, key=lambda r: r["mrr"])[:6]
    print("최저 MRR 질의:")
    for r in worst:
        print(f"  [{r['level'][:1]}|{r['id']:3}] {r['query']} — mrr={r['mrr']} r@5={r['recall@5']}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "run"))
