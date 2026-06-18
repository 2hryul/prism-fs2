# 대화 인수인계 요약 — prism-fs 검색·UX 대개편 → XBRL/사업보고서 분리 지시서

> 대상 레포: `D:\SOURCE\prism-fs` · 원격: `https://github.com/2hryul/prism-fs2.git` · 브랜치: `refactor/remove-report-fy-xbrl`
> 앱: 4대 금융지주(신한·KB·하나·우리) 별도·연결재무제표/주석 비교 대시보드. FastAPI(:8021) + 단일 `static/index.html` + PyInstaller onedir. Windows 11, 폐쇄망 친화, AI 무경유 결정론 원칙(숫자 재구성·단위환산 0).

## 1. 대화의 시작과 목표
- 시작: "현재 진행상태 확인". 기준점은 **v0.4.0(`c6f4c87`)** — 사업보고서(report)·연간(FY) 기간·XBRL을 전면 제거한 슬림 상태(커밋·푸시 완료).
- 전제/제약: Windows 11·PowerShell, 폐쇄망(외부 https 0), DART OpenAPI(`fnlttSinglAcntAll`=재무수치), 로컬 임베딩(`jhgan/ko-sroberta`), 결정론(파생값 계산식 provenance, AI 숫자생성 금지).
- 사용자 최종 기대: ① 재무제표 비교·검색 정확도와 UX를 실사용 수준으로 끌어올리고, ② 배포용 셋업 생성, ③ 제거했던 **XBRL·사업보고서**는 **별도 프로그램**으로 분리 구현하는 작업지시서 확보.

## 2. 주요 대화 흐름 및 상호작용 (시간순)
1. 진행상태·git위치 확인 → 테스트용 개발서버(:8021) 기동.
2. **FY 보강 결정**: "4사 모든 자료" 요구에 XBRL/사업보고서 재도입 검토 → 사용자 결정 **"FS 숫자만"**. fnlttSinglAcntAll을 `reprt_code=11011`로 호출해 **연간확정(FY)+다년 시계열**만 추가(report.pdf·XBRL 없음). **v0.5.0**. 2023/2024/2025FY×4사 수집.
3. **데이터 손실 이상**: 작업 중 2023FY·2024FY·2026Q3 폴더가 디스크에서 사라짐. 조사 결과 reindex/테스트는 원인 아님(전체 pytest 후에도 유지). 2023/2024FY 재수집으로 복구, **2026Q3는 미래분기 placeholder라 제외**. 원인 미규명.
4. **검색 정확도+편의** → 사용자 선택 **검색 튜닝 + PDF 원문 매칭 하이라이트**. 문장경계 청크(schema 3→4), pdf.js 텍스트레이어 형광(주의: 이 pdf.js는 `TextLayer` 클래스 미export, `renderTextLayer` 함수 사용). **v0.6.0**. 골든셋 MRR 본문 0.85 기준.
5. **온톨로지·Graph-RAG 삭제 검토** → 실데이터 무관 더미 프론트 데모로 확인, 전면 제거 + RAG 단일화. **v0.7.0**.
6. **기간 일괄 추가/삭제 버튼**(매트릭스 기간 헤더 ＋4사/🗑전체) → **v0.8.0**. (이후 #12에서 ＋4사 제거, 🗑은 헤더텍스트 클릭으로 이동.)
7. **재무제표 비교 4사 동시표시**: 회사 드롭다운 제거 → 4사 스택(**v0.9.0**) → "같은 계정으로 4사 한꺼번에"로 **계정=행·4사=열 병합**(`fsMergeByAccount`) (**v0.9.1**).
8. (스크린샷) 4사 비교의 "—"는 매칭 버그 아님 — 회사별 계정 분류 차이(실데이터). account_id 매칭 정상. 변경 없음.
9. **우측 컬럼 잘림** → `body.fs-wide` 전체폭 + **계정 컬럼 sticky** + 긴 account_id 줄바꿈. **v0.9.2**.
10. **"연결재무제표 뒤 숫자" = 인덱싱된 주석 개수**(`review_notes_count`). 재무수치/본문과 별개임을 설명.
11. **"별도 인덱싱 완료 = 주석만?"** → 인덱싱은 주석(footnote)만, 재무수치는 fnlttSinglAcntAll 별도 수집임을 설명. (신한 2025FY는 사용자가 감사보고서 PDF 업로드로 주석까지 보유.)
12. **"재무수치와 별도로 본문 내용도 수집"** → 범위 확정(사업보고서 제외, **연결/별도 두 문서만**, **PDF 전체(주석 포함)**). `build_body_index`로 전 페이지를 본문 유닛(`no="B…"`)으로 인덱싱(`index_body_*.json`), RAG 병합. **v0.10.0**. 21셀 재인덱싱.
13. **수정사항 4건**: ①업로드 기간 자동검증(결산기준일 `YYYY년 M월 DD일`→Q1/Q2/Q3/FY) ②PDF 더블클릭 원문열기 복구(텍스트레이어가 가로채던 것 → pageWrap에 핸들러) ③＋4사 버튼 삭제 ④기간 헤더텍스트 클릭=4사 일괄삭제(hover 안내). **v0.10.1**.
14. **3탭 전체 테스트 + 초보 안내**: 라이브러리/비교조회/자연어질의 기능 정상 확인 + 라이브러리 3단계 퀵스타트 배너 + 탭 용도 안내. **v0.10.2**.
15. **검색 후보 접기**: 비교 패널별 "나머지 N개 접기/펼치기". **v0.10.3**.
16. **수정 3건**: ①타이틀 "4대 금융지주 별도,연결재무제표 비교 분석" ②자연어질의=라이브러리 **선택 문서만** 검색(`cell_keys`, 연결/별도 혼재, 출처별 doc_type) ③비교 대상 칩 **드래그&드롭 정렬**. **v0.11.0**.
17. **가이드 닫기 버그 수정 + 전 탭 닫기 가능 가이드**: `.quickstart`의 display:flex가 `.hidden` 무력화 → `.quickstart.hidden,.tab-guide.hidden{display:none!important}`. 공용 `dismissGuide(key)`+localStorage, 4개 탭에 ✕. **v0.11.1**.
18. **git 커밋&푸시**: v0.5.0~0.11.1 일괄 단일 커밋 **`8e77ce5`** → origin 푸시 완료(`c6f4c87..8e77ce5`). 커밋 메시지 `@` 오타 amend 후 푸시. `.env`·`storage/` gitignore 확인.
19. **/arch — PDF 검색 정확도 검증**: `scripts/eval_search.py`(골든셋 n=25) 측정 → **본문 전체 인덱싱이 제목검색 희석(MRR 0.853→0.667, 8/25에서 본문이 정답 주석보다 1위)**. Owner 결정 **주석 우선 정렬**. Builder가 **S1** 구현(`notes_rag.retrieve` 정렬 키에 `BODY_RANK_PENALTY=0.15`, 표시점수 불변) → **MRR 0.667→0.813**, 본문 단독질의(감사의견)는 여전히 상위. Reviewer **CLEAR**. pytest 160. **(미커밋)**
20. **"보완하면 얼마나?"**: 하니스가 `doc_type` 미전달로 '별도' 과소평가 → 별도→review_sep 라우팅 측정 시 **전체 MRR 0.813→0.893**. 단 이는 **측정 도구 버그 수정(엔진 개선 아님)** — 실 UI는 이미 별도 정상 조회. 진짜 현재 정확도 ≈ **MRR 0.89/hit@1 0.84**.
21. **셋업파일 빌드**: `build.ps1`(VERSION→PyInstaller onedir, 게이트 pytest+https0) → **`dist\setup\setup_v0.11.1\setup_v0.11.1.exe`**(+`_internal`,`storage`), 총 2.46GB. **frozen 스모크 통과**(헬스200·번들모델 로드·storage 서빙). 단 번들 storage는 16셀(빌드시점 catalog), S1 포함하나 미커밋.
22. **"분리 이전 작업 기록?"** → 예: git 커밋이력(XBRL `115406b`·`d627cae`·`eb3e932`·`a375a5d`, report/문서축 `abb038d`·`c6456bc`) + 제거 소스 git 복구 가능(`c6f4c87^:src/xbrl_tagging.py` 708줄·`sections.py` 141줄·`scripts/build_xbrl_tagging.py` 154줄·테스트) + 개발노트(`개발노트_report_FY_XBRL제거_20260616.md` 등). 일부 XBRL 전용 노트는 git 안에만.
23. **작업지시서 작성**: `doc/작업지시서_XBRL_사업보고서_분리프로그램_20260618.md` 생성 — XBRL·사업보고서 전담 **별도 프로그램(prism-xbrl, 포트 8022)** 구현 + 이 세션 개선 전부 계승.
24. **"v0.4.0 별도 repo 구성함"** → 그 repo가 **신규 프로그램 베이스**로 확정. 지시서 수정: 베이스=v0.4.0 repo(XBRL/report는 그 repo 자체 이력 `c6f4c87^`에서 복구), 세션 개선은 **prism-fs `8e77ce5`에서 diff 이식**.

## 3. 지금까지 도달한 결론
- **prism-fs 본체**: v0.5.0~0.11.1 (FY·검색튜닝·PDF하이라이트·전체본문인덱싱·온톨로지제거·기간일괄버튼·4사계정비교·전체폭/sticky·후보접기·드래그정렬·선택문서검색·업로드검증·전탭가이드·타이틀변경) **커밋 `8e77ce5`로 푸시 완료**.
- **검색 정확도(측정 근거 확보)**: 본문 인덱싱이 제목검색 희석 → **S1 주석 우선 정렬**로 MRR 0.667→0.813 회복(Reviewer CLEAR, pytest 160). 하니스 별도 라우팅 보완 시 0.813→0.893. **진짜 현재 정확도 ≈ MRR 0.89**.
- **셋업**: `dist\setup\setup_v0.11.1\` onedir 번들 생성·frozen 동작 검증(2.46GB).
- **분리 지시서**: `doc/작업지시서_XBRL_사업보고서_분리프로그램_20260618.md` — 베이스=별도 v0.4.0 repo + prism-fs 8e77ce5 이식. STEP 0~7, 복구경로(git), 계승 개선 전체, 검증/빌드.
- **미해결/미커밋**:
  - **S1(주석 우선 정렬)·하니스 보완(S2) 미커밋** (작업트리에 S1만 존재: `src/notes_rag.py`+`tests/test_notes_rag_chunks.py`, handoff 파일, 작업지시서 doc).
  - 데이터 손실 원인 미규명(복구 완료, src/storage는 24셀이나 번들은 16셀).
  - 헤더 `v0.1.0` 하드코딩(VERSION 불일치, 기존 이슈).
  - 브랜치명 `refactor/remove-report-fy-xbrl`이 작업범위와 불일치(main 머지 시 PR/정리 권장).

## 4. 마지막 상태 및 다음 단계
- **마지막 요청**: 인수인계 문서 작성(본 문서). 직전: v0.4.0 repo=베이스 확정에 맞춰 작업지시서 수정 완료.
- **즉시 결정 필요**:
  1. **S1 커밋 여부**(권장 VERSION 0.11.1→0.11.2, Reviewer CLEAR). → 커밋 후 **셋업 재빌드**(setup_v0.11.2)할지, 현 setup_v0.11.1 그대로 둘지.
  2. **하니스 보완(S2)** 진행 여부(eval_search.py에 fs_div→doc_type 라우팅, 테스트도구만).
  3. 번들 데모데이터 **rescan(24셀 반영) 후 재빌드** 여부(현재 16셀).
- **신규 세션(prism-xbrl) 착수 시**: 작업지시서를 v0.4.0 repo로 복사 → STEP 0(베이스 확인+8e77ce5 개선 이식 대상 식별) → STEP 1~7. 이식은 `git diff c6f4c87 8e77ce5 -- <file>`로 세션 개선분만 병합 권장. (요청 시 파일별 정확 diff 범위 지시서에 추가 가능.)
- **서버 상태**: 개발서버 정지됨(frozen 스모크 후). 재기동 `python src\run_server.py`.

### 핵심 파일/심볼 레퍼런스
- 검색: `src/notes_rag.py`(`retrieve`, `BODY_RANK_PENALTY=0.15`), `src/app.py`(`_chunk_text`/`_split_sentences`, `build_body_index`/`body_index_path`/`_load_body_index`, `notes_rag_query`+`cell_keys`, `detect_period_from_text`+`_STMT_DATE_RE`).
- UI: `src/static/index.html`(`renderPdfRange`+`addTextLayerHighlight`, `fsMergeByAccount`, `toggleCandList`, `attachTargetDnD`/`reorderTargets`, `dismissGuide`/`.tab-guide`, `renderMatrix` 기간헤더).
- 측정/빌드: `scripts/eval_search.py`, `build.ps1`, `prism_fs.spec`, `VERSION`(0.11.1).
- 복구(분리 프로그램): `git show c6f4c87^:src/xbrl_tagging.py|src/sections.py|scripts/build_xbrl_tagging.py`.
- 개발노트: `doc/개발노트_*_20260617.md`(11건)+`개발노트_report_FY_XBRL제거_20260616.md`+`handoff/BUILD-LOG.md`(S1).
