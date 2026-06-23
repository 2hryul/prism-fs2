# 보고 — prism-fs 세션 개선분 & prism-xbrl 계승 대상

> 작성 2026-06-18 · 기준 커밋 `main` `b98b8d3` (prism-fs) · 검색 골든셋 n=25

---

## 0. 목표 산출물 정의

**prism-xbrl** — prism-fs와 **독립 실행되는 별도 프로그램**.

| 구분 | 내용 |
|---|---|
| 전담 범위 | ① **XBRL 상세태깅** (재무제표 표준계정 매핑·태그) ② **사업보고서 본문/주석** 처리 |
| 베이스 | 별도 **v0.4.0 repo** (XBRL/report 소스는 `c6f4c87^`에서 git 복구) |
| 포트 | 8022 (prism-fs 8021과 분리 동시기동) |
| 독립성 | prism-fs와 프로세스·데이터·포트 완전 분리. 단 **UI·검색 엔진은 prism-fs 이번 세션 개선분을 계승** |
| 계승 방식 | `git diff c6f4c87 8e77ce5 -- <file>`로 세션 개선 코드만 이식 |

복구 대상 소스(분리 프로그램 골격):
- `git show c6f4c87^:src/xbrl_tagging.py` (708줄)
- `git show c6f4c87^:src/sections.py` (141줄, 사업보고서 섹션 분해)
- `git show c6f4c87^:scripts/build_xbrl_tagging.py` (154줄)

---

## 1. 화면(UI) 개선 — 계승 대상

| 개선 | 내용 | 핵심 심볼 (src/static/index.html) |
|---|---|---|
| 4사 동시 비교 | 회사 드롭다운 제거 → **계정=행 · 4사=열** 병합 표시 | `fsMergeByAccount` |
| 전체폭 레이아웃 | `body.fs-wide` 전체폭 + **계정 컬럼 sticky** + 긴 account_id 줄바꿈 | `.fs-wide` |
| 검색 후보 접기 | 비교 패널별 "나머지 N개 접기/펼치기" | `toggleCandList` |
| 비교칩 정렬 | 비교 대상 칩 **드래그&드롭** 재정렬 | `attachTargetDnD` / `reorderTargets` |
| PDF 원문 하이라이트 | pdf.js 텍스트레이어 형광 + 더블클릭 원문 열기 | `renderPdfRange` / `addTextLayerHighlight` |
| 기간 일괄 관리 | 매트릭스 기간 헤더 클릭 = 4사 일괄삭제 (hover 안내) | `renderMatrix` 기간헤더 |
| 초보 가이드 | 3단계 퀵스타트 배너 + 4탭 가이드 + ✕ 닫기 | `dismissGuide` / `.tab-guide` |
| 군더더기 제거 | 온톨로지·Graph-RAG 더미 데모 전면 제거 → RAG 단일화 | — |
| 타이틀 | "4대 금융지주 별도,연결재무제표 비교 분석" | — |

> ⚠️ **함정**: 이 pdf.js는 `TextLayer` 클래스 미export → `renderTextLayer` **함수**를 사용해야 함. 더블클릭은 텍스트레이어가 가로채므로 `pageWrap`에 핸들러 부착.

---

## 2. 기능(Feature) 개선 — 계승 대상

| 개선 | 내용 | 핵심 심볼 (src/app.py) |
|---|---|---|
| 본문 전체 인덱싱 | PDF 전 페이지를 본문 유닛(`no="B…"`)으로 인덱싱 → 주석+본문 RAG 병합. 감사의견·본표 등 주석 외 본문도 검색 | `build_body_index` / `body_index_path` / `_load_body_index` |
| 선택 문서 검색 | 자연어질의 = 라이브러리 **선택 문서만** 검색(연결/별도 혼재, 출처별 doc_type) | `notes_rag_query` + `cell_keys` |
| 업로드 기간 자동검증 | 결산기준일 `YYYY년 M월 DD일` → Q1/Q2/Q3/FY 자동판정 | `detect_period_from_text` + `_STMT_DATE_RE` |
| FY 다년 시계열 | `fnlttSinglAcntAll` `reprt_code=11011`로 연간확정 다년 수집(숫자만, report·XBRL 무관) | — |
| 문장경계 청크 | 인덱스 스키마 3→4, 문장 단위 청킹 | `_chunk_text` / `_split_sentences` |
| 2문서 모델 | 연결(`review`)·별도(`review_sep`) 문서유형 분리 라우팅 | `validate_doc_type` / `index_path` / `pdf_path` |

> 결정론 원칙 유지: 숫자 재구성·단위환산 0, AI 숫자생성 금지. 주석 RAG는 **정성 텍스트 전용·인용 강제**(근거 없으면 답변 생성 안 함).

---

## 3. 검색률(정확도) 개선 — 계승 대상 ★핵심

골든셋 n=25, hit@k·MRR (개발 PC `scripts/eval_search.py`).

### 정확도 추이
| 단계 | MRR | 비고 |
|---|---|---|
| 본문 인덱싱 前 (주석만) | 0.853 | 기준점 |
| 본문 전체 인덱싱 後 | **0.667** | 본문 유닛이 제목검색 희석(8/25에서 본문이 정답 주석보다 1위) |
| **S1 주석 우선 정렬** | **0.813** | 엔진 개선 — 본문 유닛 정렬 강등 |
| S2 하니스 정합 後 측정 | **0.893** | 측정도구 보정(엔진 무관) |

### S1 — 주석 우선 정렬 (엔진 개선, 계승 핵심)
- `notes_rag.retrieve` 정렬 키에서 본문 유닛(`note_no`가 `"B"` 시작)에만 `BODY_RANK_PENALTY=0.15` 차감
- **표시 score는 불변** (정렬 키에만 적용) → provenance·UI 일관 유지
- 효과: 주석 제목검색 회복(MRR 0.667→0.813), 본문 단독질의(감사의견 등)는 여전히 상위
- 위치: `src/notes_rag.py` (`BODY_RANK_PENALTY`, `retrieve` 정렬), 회귀테스트 `tests/test_notes_rag_chunks.py::test_note_beats_body_unit_on_tie`

### S2 — 평가 하니스 doc_type 라우팅 (측정도구, 계승 선택)
- `eval_search.py`가 `fs_div`만 전달하고 `doc_type`을 항상 `review`(연결)로 둬 별도 질의 과소평가
- 수정: `fs_div="별도"→review_sep` 유도 → 측정 MRR 0.813→0.893, 별도 카테고리 1.0
- **엔진/UI 무관** — 실 UI는 이미 별도 정상 조회. 진짜 현재 정확도 ≈ **MRR 0.89 / hit@1 0.84**

### 보조 검색 메커니즘 (계승 대상)
- 동의어 확장 `synonyms.expand_query` (회계 동의어 그룹, 정규화 매칭)
- BM25 하이브리드 (`USE_BM25`, `COS_W_POLICY`)
- 한국어 형태소 토큰화 `tokenize_korean`

---

## 4. prism-xbrl 계승 체크리스트

신규 프로그램이 위 개선을 빠짐없이 가져가도록:

- [ ] **검색 엔진**: `notes_rag.py`(S1 포함) + `synonyms.py` + 본문 인덱싱(`build_body_index` 계열) 이식
- [ ] **UI**: `index.html`의 4사 비교·sticky·후보접기·DnD·PDF 하이라이트·가이드 이식
- [ ] **2문서 모델**: `validate_doc_type`/`index_path`/`pdf_path` 라우팅 + **사업보고서 doc_type 신설**(prism-xbrl 고유)
- [ ] **XBRL 상세태깅**: `c6f4c87^`에서 `xbrl_tagging.py`·`build_xbrl_tagging.py` 복구 후 현행 카탈로그/인덱스 모델에 맞게 갱신
- [ ] **사업보고서 섹션**: `sections.py` 복구 + 본문 전체 인덱싱 파이프라인과 결합
- [ ] **평가 하니스**: `eval_search.py`(S2 포함) 이식, 골든셋을 사업보고서/XBRL 항목으로 확장
- [ ] **결정론·인용강제 원칙** 유지

---

## 5. 현재 상태 (prism-fs 본체)

| 항목 | 값 |
|---|---|
| 브랜치 | `main` `b98b8d3` (정리 완료, origin 동기화) |
| VERSION | 0.11.2 |
| 셋업 | `dist/setup/setup_v0.11.2` (2.46GB, frozen 스모크·S1 반영 검증) |
| 검색 정확도 | MRR ≈ 0.89 / hit@1 0.84 |
| 잔여(본체) | 헤더 `v0.1.0` 하드코딩(index.html:235), 데이터 손실 근본원인 미규명 |

prism-fs 본체는 배포 가능 상태. prism-xbrl은 **별도 v0.4.0 repo 경로 확보 후** 본 보고 §4 체크리스트로 착수.
