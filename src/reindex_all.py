# -*- coding: utf-8 -*-
"""라이브러리 전체 재인덱싱 — 디스크의 모든 작업본 PDF 를 현행 스키마로 다시 인덱싱.

실행(프로젝트 루트):
    python src\\reindex_all.py [--no-llm] [--only 회사/기간] [--extras-only]
- --no-llm      : Ollama 노트 보정 생략(속도 우선 — 휴리스틱 추출 그대로 인덱싱)
- --only        : 특정 셀만(예: --only 신한/2025Q3)
- --extras-only : 주석 재인덱싱 없이 부가 인덱스(비주석 섹션·XBRL 크로스링크)만 생성
서버와 동시 실행 금지(인덱스 파일 쓰기 경합). 진행 로그를 stdout 으로 출력.
"""
import sys
import io
import time
import asyncio
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


async def main():
    args = sys.argv[1:]
    if "--no-llm" in args:
        app._OLLAMA_AVAILABLE = False  # 보정 생략 — 추출 휴리스틱 결과 그대로
        print("[reindex] LLM 보정 OFF (--no-llm)")
    only = None
    if "--only" in args:
        only = args[args.index("--only") + 1]

    if "--extras-only" in args:
        # 주석 인덱스는 유지하고 섹션·XBRL 링크만 (재)생성 — Phase3 백필용.
        cells = [(d.parent.name, d.name) for d in sorted(app.LIBRARY_ROOT.glob("*/*"))
                 if not only or f"{d.parent.name}/{d.name}" == only]
        print(f"[extras] 대상 {len(cells)}셀 (섹션+XBRL 링크)")
        for i, (company, period) in enumerate(cells, 1):
            t1 = time.time()
            try:
                sec = await app.build_sections_index(company, period)
                xl = await app.build_xbrl_links(company, period)
                print(f"[{i}/{len(cells)}] {company}/{period} — 섹션 {sec.get('sections', 0)}개"
                      f"(청크 {sec.get('chunks', 0)}), XBRL 연결주석 {xl.get('linked_notes', 0)}개, "
                      f"{time.time() - t1:.0f}s")
            except Exception as e:
                print(f"[{i}/{len(cells)}] {company}/{period} — 예외: {type(e).__name__}: {e}")
        return

    targets = []
    for cell_dir in sorted(app.LIBRARY_ROOT.glob("*/*")):
        company, period = cell_dir.parent.name, cell_dir.name
        if only and f"{company}/{period}" != only:
            continue
        for dt in ("report", "review", "review_sep"):
            if app.pdf_path(company, period, dt).exists():
                targets.append((company, period, dt))

    print(f"[reindex] 대상 {len(targets)}건 (schema v{app.INDEX_SCHEMA})")
    ok, fail = 0, 0
    t0 = time.time()
    for i, (company, period, dt) in enumerate(targets, 1):
        t1 = time.time()
        try:
            await app.index_entry(company, period, dt)
            st = app.INDEX_STATUS.get(f"{company}/{period}/{dt}", {})
            if st.get("status") == "done":
                ok += 1
                idx_mb = app.index_path(company, period, dt).stat().st_size / 1e6
                print(f"[{i}/{len(targets)}] {company}/{period}/{dt} — "
                      f"주석 {st.get('notes_extracted')}개, {idx_mb:.1f}MB, "
                      f"{time.time() - t1:.0f}s")
            else:
                fail += 1
                print(f"[{i}/{len(targets)}] {company}/{period}/{dt} — 실패: "
                      f"{st.get('error')}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(targets)}] {company}/{period}/{dt} — 예외: "
                  f"{type(e).__name__}: {e}")
    print(f"[reindex] 완료 — 성공 {ok} / 실패 {fail}, 총 {(time.time() - t0) / 60:.1f}분")


if __name__ == "__main__":
    asyncio.run(main())
