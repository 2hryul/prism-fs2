# 개발노트 — 사업보고서(report)·FY 기간·XBRL 전면 제거

## 2026-06-16 작업 내역

### 작업 내용
4대 금융지주 주석/재무제표 비교 대시보드(prism-fs)에서 다음 3개 영역을 코드·데이터·UI에서 전면 제거.

1. **사업보고서 본문(`report` doc_type)** — 토큰까지 완전 제거. 기본 doc_type을 `review`(연결재무제표)로 전환. `VALID_DOC_TYPES = {review, review_sep}`.
2. **FY(연간, reprt_code 11011) 기간** — `PERIOD_PATTERN`을 `^\d{4}Q[1-3]$`로 축소. 남는 기간 Q1/Q2/Q3.
3. **XBRL 전부** — 실제 XBRL 파싱/태깅(`xbrl_tagging`) + 계정↔주석 임베딩 링크(`build_xbrl_links`/`xbrl_links`) 둘 다.
   - 부수: `report.pdf` 비주석 본문 섹션 인덱스(`sections.py`/`build_sections_index`/`index_sections.json`)도 입력 소스 소멸로 함께 제거.

### 변경 파일
- **삭제**: `src/xbrl_tagging.py`, `scripts/build_xbrl_tagging.py`, `src/sections.py`, `tests/test_xbrl_tagging.py`, `tests/test_sections.py`
- **`src/app.py`**: VALID_DOC_TYPES 축소·기본값 review, pdf_path/index_path report 분기 제거, FY 패턴/매핑/정렬 제거, detect_* report·FY 분기 제거, upload/embed/collect의 report 분기 제거, `/api/xbrl-tagging`·`/api/xbrl-tagging/matrix`·`build_xbrl_links`·`_load_xbrl_links`·`build_sections_index`·`sections_index_path` 삭제, compare의 fs_links 부착 제거, `_doc_indexed` 헬퍼 추가(review_indexed 기반), notes_rag/terms_suggest의 indexed 플래그·doc_type 기본값 정정.
- **`src/collect_dart.py`**: fnlttXbrl 수집(`xbrl/` 해제)·`extract_xbrl_labels`·`find_lab_ko_in_zip`·`pick_report_pdf`·`fetch_report_attachment` 삭제, 11011/FY 매핑·`list_date_window` FY 블록·`--report-pdf` 인자·self-test XBRL·report PDF 블록 제거. `unzip_to`는 document.xml(source/) 해제에 쓰여 유지.
- **`src/reindex_all.py`**: `--extras-only`(sections/xbrl_links) 모드 제거, doc_type 루프 report 제거.
- **`src/fs_compare.py`**: `cmp_col`의 FY 분기(죽은 코드) 제거.
- **`src/notes_rag.py`**: `extract_note_text` 기본 doc_type review·파일명 매핑(review/review_sep), app 호출부에 doc_type 전달.
- **`src/static/index.html`**: XBRL 탭/패널/JS 전부, 비교탭 fs_links 칩, 재무제표탭 ⑧주석 정합참조(refs), FY 기간 옵션·정렬·라벨, report 문서유형(체크박스·드롭다운·DOC_* 맵·reportDocLabel·detect 분기) 제거. 기본 문서유형 review로 통일.
- **테스트**: test_collect_doc_selection / test_collect_status_details / test_delete_doc / test_doc_type_review_sep / test_pdf_detect / test_fs_compare / test_library_transfer를 review/review_sep·Q기간 기준으로 수정.
- **storage 데이터 삭제**: FY 디렉토리 4개, xbrl/ 12개, xbrl_tagging.json 12, xbrl_links.json 12, report.pdf 12, index.json 12, index_sections.json 12, report 본문 원본 PDF 12. 이후 `rescan`으로 `catalog.json` 재생성(16셀, FY·report 필드 없음).
- **기타**: `VERSION` 0.3.0→0.4.0, `README.md` 갱신, `requirements.txt` lxml 주석(OpenDartReader 전이 의존으로 유지).

### 결정 사항
- **report 토큰 완전 제거(A2)**: 사용자가 "전면 삭제" 요구 → 구조 키로 잔존시키지 않고 제거. 결과적으로 본문 주석 검색은 사라지고 연결/별도 검토보고서 주석만 검색 대상.
- **FY 디렉토리 통째 삭제(B1)**: FY 셀 내 review/review_sep 포함 전체 삭제(사용자가 "FY 기간 자체 제거" 선택).
- **lxml 유지**: 자체 코드는 표준 `xml.etree` 사용하나 OpenDartReader 하드 의존성이라 제거 불가.
- **fs_structured.json 보존**: fnlttSinglAcntAll(재무데이터) 산출물로 XBRL과 무관 → 재무제표 비교 탭이 사용하므로 유지.

### 검증
- `python -m pytest tests -q` → **142 passed, 4 skipped** (실패 0).
- `python src/collect_dart.py --self-test` → 전부 통과. `--dry-run` 정상(호출 계획에서 fnlttXbrl/report 제거 확인).
- `python -c "import app"` → ImportError 0(제거 함수 참조 잔재 없음).
- 서버 기동 스모크: `/api/xbrl-tagging` 404, `/api/library` periods에 FY 없음(Q2/Q3/Q1/Q3), `/api/notes/rag`(doc_type=review) 실데이터 응답.
- UI(preview, 캐시 새로고침): 탭 5개(XBRL 없음), 프리셋 문서에 사업보고서 없음, 기간 컬럼 FY 없음, 콘솔 에러 0.

### 미완료/다음 작업
- 헤더 하드코딩 버전 문자열이 `v0.1.0`로 고정(VERSION 파일과 불일치) — 기존부터 있던 별개 이슈. 차후 VERSION 주입으로 통일 검토.
- `spec_20260601_0937.md` / `doc/코드구조_*.md`의 XBRL·사업보고서·FY 서술은 이력 문서 성격이라 미수정(필요 시 후속 정리).

### 이슈/특이사항
- 디스크의 `storage/library`는 gitignore 대상(미추적)이라 데이터 삭제는 직접 unlink/rmtree로 수행.
- 잔존 dev 산출물(`index_heuristic.bak.json`, `notes_auto.json`)은 제거 범위 외라 보존.
