"""
4대 금융지주 주석 비교 에이전트 — FastAPI 백엔드 (PoC v2 · 라이브러리 모델, LLM API 미사용)

특징:
- 영속 라이브러리 (회사 × 연도분기)
- 임베딩 백엔드 자동 감지: 로컬 sentence-transformers > API > bigram fallback
- BM25 하이브리드 매칭 (가중치 0.7 임베딩 + 0.3 BM25)
- LLM API 키 없이도 정상 동작

실행:
    pip install -r requirements.txt
    # (권장) 로컬 임베딩 모델 사전 다운로드:
    #   python -c "from sentence_transformers import SentenceTransformer; \\
    #              SentenceTransformer('jhgan/ko-sroberta-multitask').save('./models/ko-sroberta')"
    #   export EMBED_MODEL_PATH=./models/ko-sroberta
    uvicorn main:app --reload --port 8000

API 엔드포인트:
    POST   /api/library/upload                    - 단일 (회사, 기간) PDF 업로드
    GET    /api/library                            - 전체 카탈로그 매트릭스 조회
    GET    /api/library/{company}/{period}        - 단일 항목 메타
    DELETE /api/library/{company}/{period}        - 단일 항목 삭제
    POST   /api/library/index/{company}/{period}  - (재)인덱싱 시작
    GET    /api/library/index/status              - 전체 인덱싱 상태 매트릭스
    POST   /api/compare                            - 비교 대상 명시 후 검색
    GET    /api/pdf?company=&period=               - PDF 스트리밍 (PDF.js 사용)
"""

import os
import re
import sys
import json
import shutil
import hashlib
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from contextlib import asynccontextmanager
from urllib.parse import quote

import fitz  # PyMuPDF
import httpx
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import collect_dart as cdart  # DART 수집 로직 재사용(동일 storage 레이아웃)
import notes_rag  # 주석 RAG(§5.4, 옵트인) — 정성 텍스트 전용, 숫자 무경유·인용 강제
import note_filters  # 주석 종류(주기/서술형) 결정론 필터
import note_topics  # §5.2 표준 주제 매핑(임베딩 분류, AI 무경유)
import safety  # 시크릿 마스킹·provenance 중앙화
import synonyms  # 회계 동의어 쿼리 확장(BM25/lexical, 결정론)

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
# CWD/frozen 비의존: paths 모듈이 dev·PyInstaller 양쪽 경로를 일관 해석.
import paths  # noqa: E402
BASE_DIR = paths.BUNDLE_DIR
STORAGE_ROOT = paths.STORAGE_ROOT
LIBRARY_ROOT = paths.LIBRARY_ROOT
CATALOG_PATH = STORAGE_ROOT / "catalog.json"
# Step 7: 주제사전 자동초안(build_topic_dict.py 산출). 있으면 coverage 토픽 소스로 사용.
TOPIC_DICT_PATH = STORAGE_ROOT / "topic_dict.json"
# 회계기준서 — 4개사/기간과 무관한 독립 문서공간(임의 업로드 PDF). 전용 카탈로그·디렉터리.
STANDARDS_ROOT = paths.STANDARDS_ROOT
STANDARDS_CATALOG_PATH = STANDARDS_ROOT / "catalog.json"
LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
STANDARDS_ROOT.mkdir(parents=True, exist_ok=True)

VALID_COMPANIES = {"신한", "KB", "하나", "우리"}
# 분기(Q1~Q3, 검토=잠정) + 연간확정(FY, 사업보고서 기준 fnlttSinglAcntAll).
# FY 셀은 재무데이터만 보유(PDF/주석/XBRL 없음).
PERIOD_PATTERN = re.compile(r"^\d{4}(Q[1-3]|FY)$")

# 매칭 임계값 (env 오버라이드 가능) — 순수 규칙, AI 미사용.
# 단일 점수만으로는 오답/정답 구분 불가(리스→사채 0.7 vs 공정가치 0.735)하여
# 점수 플로어 + 어휘 일치(lexical_hit) 두 신호를 함께 사용한다.
# 검색개선 Phase1: 임계 0.45→0.35 완화(재현율 우선) — UI 는 confidence 로 고/저 구분 표시.
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.35"))  # 최소 채택 점수
LEXICAL_FLOOR   = float(os.getenv("LEXICAL_FLOOR", "0.28"))    # 어휘 일치 시 완화 하한
HIGH_CONF       = float(os.getenv("HIGH_CONF", "0.65"))        # 고신뢰 하한

# 검색개선 Phase1 — 결과 폭·다중 청크 가산·BM25 제목 신호 보존.
# 기본값은 골든셋 스윕(doc\eval\eval_sweep_*.json)으로 확정: u0.8/b0.2 가
# 제목 MRR +0.04 이면서 본문 MRR 무손실(0.818 vs 0.823)·R@5 양 레벨 개선.
SEARCH_TOP_K  = int(os.getenv("SEARCH_TOP_K", "10"))           # compare 회사당 후보 상한(5→10)
UNIT_MAX_W    = float(os.getenv("UNIT_MAX_W", "0.8"))          # 유닛 max 가중(잔여=상위3 평균)
BM25_TITLE_W  = float(os.getenv("BM25_TITLE_W", "0.2"))        # BM25 제목 코퍼스 가중(잔여=청크)

# B-1/B-2 (note_kind 인지 검색): 주기(회계정책·작성기준·일반사항 등 prose) 주석은 제목·본문
# 키워드가 빈약해 BM25·lexical 신호가 약하고 XBRL 미태깅이 많다(진단 pdf_only로 확인). 따라서
#   ① 하이브리드 가중을 임베딩(의미매칭) 쪽으로 올리고 ② 채택 임계를 완화한다.
# 서술형(항목공시: 파생상품·대출채권 등 표·키워드 신호 보유)은 현행 유지(0.7/0.3·0.45) — 무회귀.
COS_W_DEFAULT    = float(os.getenv("COS_W_DEFAULT", "0.7"))    # 서술형·기타: 임베딩 가중
BM_W_DEFAULT     = float(os.getenv("BM_W_DEFAULT", "0.3"))     # 서술형·기타: BM25 가중
COS_W_POLICY     = float(os.getenv("COS_W_POLICY", "0.85"))    # 주기(정책 서술): 의미매칭 비중↑
BM_W_POLICY      = float(os.getenv("BM_W_POLICY", "0.15"))
POLICY_MIN_MATCH = float(os.getenv("POLICY_MIN_MATCH", "0.30"))  # 주기 채택 하한(완화, Phase1 0.40→0.30)

# Step 4 — 신한 인사이트(커버리지/차집합)용 상수
# DEFAULT_TOPICS: 횡단 커버리지 매트릭스 기본 주제 목록.
#   주의: 회계팀 확정 Top 주제가 아닌 "플레이스홀더" — 확정 리스트로 교체 전제.
DEFAULT_TOPICS = [
    "공정가치", "대손충당금", "금융상품 위험", "영업권", "리스",
    "확정급여", "법인세", "우발부채 및 약정", "특수관계자 거래", "자본",
]
# GAP_SIM_THRESHOLD: 구조 차집합(B)에서 두 회사의 note 가 "대응"되는지 판정할 코사인 하한.
#   한국어 임베딩 anisotropy(무관 쌍 baseline ~0.3-0.5, 진짜 매치 0.7+)를 고려해 실데이터로 보정.
GAP_SIM_THRESHOLD = float(os.getenv("GAP_SIM_THRESHOLD", "0.62"))

# 회사·기간별 인덱싱 상태 (key: "{company}/{period}")
INDEX_STATUS: Dict[str, Dict[str, Any]] = {}

# 회사·기간별 DART 수집 상태 (key: "{company}/{period}")
COLLECT_STATUS: Dict[str, Dict[str, Any]] = {}

# 영문 Word 매핑 작업 상태 (key: job_id)
WORDMAP_STATUS: Dict[str, Dict[str, Any]] = {}

# 라이브러리 period 접미 → DART reprt_code. FY=사업보고서(11011, 재무데이터만 수집).
_SUFFIX_TO_REPRT = {"Q1": "11013", "Q2": "11012", "Q3": "11014", "FY": "11011"}

# 임베딩 백엔드 자동 감지 (LLM API 없이도 동작)
# frozen 번들이면 동봉 모델 폴더 우선(오프라인). 그 외엔 HF 식별자(개발).
_DEFAULT_MODEL = str(paths.MODEL_DIR) if paths.MODEL_DIR.exists() else "jhgan/ko-sroberta-multitask"
EMBED_MODEL_PATH = os.getenv("EMBED_MODEL_PATH", _DEFAULT_MODEL)
USE_LOCAL_EMBED = False
USE_BM25 = os.getenv("USE_BM25", "true").lower() == "true"
USE_API = bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY"))

_embed_model = None
try:
    if not USE_API:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL_PATH)
        USE_LOCAL_EMBED = True
except Exception as _e:
    print(f"[warn] sentence-transformers 로드 실패 → bigram fallback 사용: {_e}")

# 질의 임베딩 차원. 로컬 모델(ko-sroberta)=768, bigram 폴백=512.
# 인덱스는 생성 시 백엔드 차원으로 고정되므로, 로드 실패로 백엔드가 바뀌면
# 질의(512) ↔ 인덱스(768) 차원 불일치로 검색이 깨진다 → 아래 핸들러가 명확히 안내.
EMBED_DIM = 768 if USE_LOCAL_EMBED else 512


def _sample_index_dim() -> Optional[int]:
    """라이브러리에서 첫 인덱스 1개의 임베딩 차원을 반환(없으면 None). 기동 경고용."""
    try:
        for idx_file in paths.LIBRARY_ROOT.glob("**/index*.json"):
            data = json.loads(idx_file.read_text(encoding="utf-8"))
            notes = data.get("notes", data) if isinstance(data, dict) else data
            for note in (notes or []):
                emb = note.get("embedding")
                if emb:
                    return len(emb)
            return None  # 인덱스는 있으나 임베딩 부재 — 추가 탐색 불필요
    except Exception:
        return None
    return None

try:
    from rank_bm25 import BM25Okapi
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False
    USE_BM25 = False

# Ollama LLM 보정 (선택적 — 정확도 향상용)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# OpenAI 옵트인 — .env 키 존재 시 우선 사용(없으면 로컬 Ollama). 키는 헤더로만, 마스킹.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
USE_OLLAMA = os.getenv("USE_OLLAMA", "auto").lower()  # auto / true / false
_OLLAMA_AVAILABLE = False


# ----------------------------------------------------------------------------
# FastAPI 앱
# ----------------------------------------------------------------------------
async def check_ollama() -> bool:
    """Ollama 서버 가용성 확인 + 모델 존재 여부 검증."""
    global _OLLAMA_AVAILABLE
    if USE_OLLAMA == "false":
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code != 200:
                return False
            models = [m["name"] for m in r.json().get("models", [])]
            if not any(OLLAMA_MODEL.split(":")[0] in m for m in models):
                print(f"[warn] Ollama 모델 미발견: {OLLAMA_MODEL}. 'ollama pull {OLLAMA_MODEL}' 실행 필요")
                return False
            _OLLAMA_AVAILABLE = True
            return True
    except Exception as e:
        if USE_OLLAMA == "true":
            print(f"[warn] Ollama 강제 활성화 설정이지만 연결 실패: {e}")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] library: {LIBRARY_ROOT.resolve()}")
    print(f"[startup] embedding backend: " + (
        f"local model ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "API" if USE_API
        else "bigram fallback (저정확도)"
    ))
    print(f"[startup] BM25 하이브리드: {USE_BM25 and _HAS_BM25}")
    # 백엔드(질의)와 기존 인덱스 차원이 어긋나면 검색이 전부 깨지므로 기동 시 선제 경고.
    if not USE_LOCAL_EMBED:
        idx_dim = _sample_index_dim()
        if idx_dim and idx_dim != EMBED_DIM:
            print(f"[CRITICAL] 임베딩 차원 불일치: 질의={EMBED_DIM}(bigram 폴백) ↔ "
                  f"인덱스={idx_dim}. 모델 로드 실패 상태입니다. 검색이 실패합니다 — "
                  "모델 동봉 exe로 실행하거나 sentence-transformers/모델 경로를 확인하세요.")
    ollama_ok = await check_ollama()
    print(f"[startup] Ollama LLM 보정: {'ON (' + OLLAMA_MODEL + ')' if ollama_ok else 'OFF'}")
    yield


app = FastAPI(title="주석 비교 에이전트 v2 (라이브러리)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def _value_error_handler(request, exc: ValueError):
    """임베딩 차원 불일치(질의 ↔ 인덱스)를 cryptic 500 대신 원인·조치를 담은 503으로 변환.

    np.dot(질의 512, 인덱스 768) 등 shape 불일치는 임베딩 모델 로드 실패로 bigram(512)
    폴백 중인데 인덱스는 모델(768)로 생성된 경우 발생한다. 그 외 ValueError 는 내부 구조
    노출을 막기 위해 일반 500 메시지로 처리.
    """
    msg = str(exc)
    if "not aligned" in msg or ("shapes" in msg and "dim" in msg):
        backend = "local-model(768)" if USE_LOCAL_EMBED else "bigram-fallback(512)"
        return JSONResponse(status_code=503, content={"detail": (
            f"임베딩 차원 불일치 — 검색 인덱스와 질의 임베딩 차원이 다릅니다(현재 백엔드: {backend}, "
            f"질의 차원: {EMBED_DIM}). 임베딩 모델(ko-sroberta) 로드 실패로 bigram(512) 폴백 중일 "
            "가능성이 높습니다. 모델이 동봉된 setup_v*.exe 로 실행하거나, 개발 모드에서는 "
            "sentence-transformers 설치·모델(src/models/ko-sroberta) 경로를 확인 후 서버를 재시작하세요."
        )})
    return JSONResponse(status_code=500, content={"detail": "내부 처리 오류가 발생했습니다."})



# ----------------------------------------------------------------------------
# 유틸 — 경로·검증·카탈로그
# ----------------------------------------------------------------------------
def validate_period(period: str) -> str:
    if not PERIOD_PATTERN.match(period):
        raise HTTPException(400, f"기간 형식 오류: {period} (예: 2025Q3, 2025FY)")
    return period


# 기간 정렬키 — 연도 + 분기순(Q1<Q2<Q3<FY). FY(연간확정)는 같은 해 분기들 뒤(연말).
_PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}


def period_sort_key(period: str):
    try:
        return (int(period[:4]), _PERIOD_ORDER.get(period[4:], 9))
    except (ValueError, IndexError):
        return (9999, 9)


def validate_company(company: str) -> str:
    if company not in VALID_COMPANIES:
        raise HTTPException(400, f"알 수 없는 회사: {company}")
    return company


def entry_dir(company: str, period: str) -> Path:
    return LIBRARY_ROOT / validate_company(company) / validate_period(period)


# ── doc_type 3문서 모델 ──────────────────────────────────────────────────────
# 한 (회사,기간) 셀이 review(연결재무제표 검토보고서)·review_sep(별도재무제표 검토보고서)
# ·report(사업/분기/반기보고서 본문) 를 보유. 모든 신규 파라미터 기본 "review"(연결).
# report 는 연결·별도 혼재(fs_div 중립) 문서로, 검색 시 fs_div="all" 로 다룬다.
VALID_DOC_TYPES = {"review", "review_sep", "report"}


def validate_doc_type(doc_type: Optional[str]) -> str:
    """doc_type 검증 — 미지정(None/빈값)이면 "review"(연결재무제표) 기본.

    허용 목록(whitelist) 방식. 그 외 값은 400 으로 즉시 차단.
    """
    if not doc_type:
        return "review"
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(400, f"알 수 없는 문서유형: {doc_type} (review|review_sep|report)")
    return doc_type


def pdf_path(company: str, period: str, doc_type: str = "review") -> Path:
    """문서유형별 작업본 PDF 경로. review→review.pdf, review_sep→review_sep.pdf, report→report.pdf."""
    dt = validate_doc_type(doc_type)
    filename = {"review_sep": "review_sep.pdf", "report": "report.pdf"}.get(dt, "review.pdf")
    return entry_dir(company, period) / filename


def index_path(company: str, period: str, doc_type: str = "review") -> Path:
    """문서유형별 인덱스 경로. review→index_review.json, review_sep→index_review_sep.json, report→index_report.json."""
    dt = validate_doc_type(doc_type)
    filename = {"review_sep": "index_review_sep.json",
                "report": "index_report.json"}.get(dt, "index_review.json")
    return entry_dir(company, period) / filename


# 표준 작업본 파일명 — 원본명 폴백 스캔에서 반드시 제외(표준본을 "원본"으로 오인 방지).
_STANDARD_PDF_NAMES = {"review.pdf", "review_sep.pdf", "report.pdf"}


def _filename_matches_doc_type(name: str, doc_type: str) -> bool:
    """bracketed 원본 PDF 파일명이 doc_type 에 해당하는지 키워드로 판정.

    - review      : 연결검토 포함(연결재무제표 검토보고서).
    - review_sep  : 검토 포함 & 연결 미포함(별도재무제표 검토보고서).
    - report      : 사업/분기/반기보고서 포함 & 검토·감사 미포함(본문 보고서).
    """
    if doc_type == "review":
        return "연결검토" in name
    if doc_type == "review_sep":
        return "검토" in name and "연결" not in name
    if doc_type == "report":
        return (("사업보고서" in name or "분기보고서" in name or "반기보고서" in name)
                and "검토" not in name and "감사" not in name)
    return False


def original_pdf_name(company: str, period: str, doc_type: str) -> Optional[str]:
    """문서유형별 '원본' PDF 파일명을 추정. 없으면 None.

    ① meta.json 의 documents[] 에서 doc_type 매칭 항목의 filename_original 우선.
    ② 없으면 entry_dir 내 *.pdf 스캔 폴백(키워드 매칭). 표준 작업본(report.pdf 등)은 제외.
    표시·다운로드용 원본명 노출 목적 — 작업본 파일명(report.pdf)을 그대로 노출하지 않기 위함.
    """
    dt = validate_doc_type(doc_type)
    d = entry_dir(company, period)

    # ① meta.json documents[] 우선
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for doc in meta.get("documents", []):
                fn = doc.get("filename_original")
                # 표준 작업본명(report.pdf 등)이 meta 에 잘못 들어간 경우 방어 — 폴백 스캔으로.
                if doc.get("doc_type") == dt and fn and fn.lower() not in _STANDARD_PDF_NAMES:
                    return fn
        except (OSError, json.JSONDecodeError):
            pass  # meta 손상/접근 불가 → 폴백 스캔으로 진행

    # ② 디렉터리 *.pdf 스캔 폴백 — 표준 작업본 3종 제외 후 키워드 매칭.
    try:
        for pdf in sorted(d.glob("*.pdf")):
            if pdf.name.lower() in _STANDARD_PDF_NAMES:
                continue
            if _filename_matches_doc_type(pdf.name, dt):
                return pdf.name
    except OSError:
        pass  # 디렉터리 접근 불가 → None

    return None


def load_catalog() -> dict:
    if not CATALOG_PATH.exists():
        return {"updated_at": None, "entries": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict):
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upsert_catalog_entry(company: str, period: str, **fields):
    cat = load_catalog()
    cat["entries"] = [
        e for e in cat["entries"]
        if not (e["company"] == company and e["period"] == period)
    ]
    entry = {"company": company, "period": period, **fields}
    cat["entries"].append(entry)
    save_catalog(cat)


def remove_catalog_entry(company: str, period: str):
    cat = load_catalog()
    cat["entries"] = [
        e for e in cat["entries"]
        if not (e["company"] == company and e["period"] == period)
    ]
    save_catalog(cat)


def _strip_doc_fields(entry: dict, doc_type: str) -> dict:
    """카탈로그 엔트리에서 해당 doc_type 의 필드만 제거(다른 문서·재무데이터 보존).

    주의(접두 충돌): review_sep_* 도 'review_' 로 시작하므로, review 제거 시
    review_sep_* 는 반드시 보존해야 한다(아니면 연결 삭제가 별도 데이터까지 날림).
    """
    out = dict(entry)
    if doc_type == "review_sep":
        for k in [k for k in out if k.startswith("review_sep_")]:
            out.pop(k, None)
        out.pop("review_sep_collected", None)
    elif doc_type == "report":
        # report_* 제거. 단 report_nm(DART 보고서명 메타)은 doc_type 필드가 아니므로 보존.
        for k in [k for k in out if k.startswith("report_") and k != "report_nm"]:
            out.pop(k, None)
        out.pop("report_collected", None)
    else:  # review
        for k in [k for k in out
                  if k.startswith("review_") and not k.startswith("review_sep_")]:
            out.pop(k, None)
        out.pop("review_collected", None)
    return out


# ----------------------------------------------------------------------------
# 임베딩·토큰화
# ----------------------------------------------------------------------------
def _bigram_embedding(text: str) -> np.ndarray:
    """최후의 fallback — 모델·API 모두 없을 때만 사용 (저정확도)."""
    text = (text or "").lower()
    vec = np.zeros(512, dtype=np.float32)
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        idx = hash(bigram) % 512
        vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _local_embedding_sync(text: str) -> np.ndarray:
    return _embed_model.encode(text, normalize_embeddings=True).astype(np.float32)


def _local_embedding_batch_sync(texts: List[str]) -> np.ndarray:
    """배치 인코딩 — 인덱싱(노트당 수십 청크)에서 단건 호출 대비 수십 배 빠름(CPU 포함)."""
    return _embed_model.encode(texts, normalize_embeddings=True,
                               batch_size=32).astype(np.float32)


async def make_embedding(text: str) -> np.ndarray:
    """임베딩 생성 — 우선순위: 로컬 모델 > API > bigram fallback."""
    if USE_LOCAL_EMBED:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _local_embedding_sync, text)
    if USE_API:
        # 실제 운영: from openai import AsyncOpenAI; ...
        return _bigram_embedding(text)
    return _bigram_embedding(text)


async def make_embeddings(texts: List[str]) -> np.ndarray:
    """복수 텍스트 일괄 임베딩 (인덱싱 경로 전용) — make_embedding 과 동일 백엔드."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    if USE_LOCAL_EMBED:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _local_embedding_batch_sync, texts)
    return np.stack([_bigram_embedding(t) for t in texts])


def _round_emb(vec) -> list:
    """저장용 벡터 반올림(EMB_ROUND 자리) — JSON 크기 절감. 코사인 오차 <1e-4 (무시 가능)."""
    return [round(float(x), EMB_ROUND) for x in vec]


_KOREAN_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)

# Phase B: 형태소 토크나이저(kiwipiepy). 내용형태소만 유지(조사·어미·문장부호 제거).
_KIWI = None
_KIWI_TRIED = False
# 명사(NNG/NNP)·외국어(SL)·한자(SH)·숫자(SN)·동사/형용사 어간(VV/VA)·어근(XR)
_KIWI_KEEP = {"NNG", "NNP", "SL", "SH", "SN", "VV", "VA", "XR"}


# A-2(kiwi 사용자사전)·B-3(주기 청킹) 실험은 2026-06-04 실측에서 교차변형 재현율 회귀
# (골든 1.0→0.88, 동의어-교차 1.0→0.571)로 롤백함 — 세밀 형태소 분할이 이 코퍼스의
# 교차변형 BM25 중첩(공유 sub-토큰)에 더 유리. 개발노트 참조.
def _get_kiwi():
    """kiwipiepy 싱글톤(최초 1회 로드, 폐쇄망 오프라인). 미설치/실패 시 None → 정규식 폴백."""
    global _KIWI, _KIWI_TRIED
    if _KIWI_TRIED:
        return _KIWI
    _KIWI_TRIED = True
    try:
        from kiwipiepy import Kiwi
        _KIWI = Kiwi()
    except Exception as e:
        print(f"[warn] kiwipiepy 로드 실패 → 정규식 토크나이저 폴백: {e}")
        _KIWI = None
    return _KIWI


def tokenize_korean(text: str) -> list:
    """형태소 토크나이저 — kiwipiepy 설치 시 내용형태소 추출(조사 분리로 BM25 정밀↑),
    미설치 시 정규식 폴백. 인덱싱·질의 양쪽에서 동일 사용(코퍼스 일관)."""
    if not text:
        return []
    kiwi = _get_kiwi()
    if kiwi is not None:
        try:
            return [t.form for t in kiwi.tokenize(text)
                    if t.tag in _KIWI_KEEP and len(t.form) > 1]
        except Exception:
            pass  # 런타임 실패 시 정규식 폴백
    return [t for t in _KOREAN_TOKEN_RE.findall(text) if len(t) > 1]


# ----------------------------------------------------------------------------
# 1) Library — Upload / List / Get / Delete
# ----------------------------------------------------------------------------
def safe_original_filename(name: Optional[str]) -> Optional[str]:
    """업로드 원본 파일명을 디스크 저장용으로 정규화.

    file.filename 은 외부 입력 → 디렉터리 성분 제거(경로 트래버설 차단) +
    Windows 금지문자 치환. report.pdf(작업본)와 충돌 방지. None/빈값이면 None.
    """
    if not name:
        return None
    base = os.path.basename(name.replace("\\", "/"))      # 경로 성분 제거 → 파일명만
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(". ")
    if not base:
        return None
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    if base.lower() in _STANDARD_PDF_NAMES:    # 작업본(review/review_sep/report)과 충돌 방지
        base = "original_" + base
    return base


# ── 업로드 PDF 자동 감지(회사/기간/문서유형) ────────────────────────────────
# 파일명·1페이지 텍스트로 회사/기간/문서유형을 추정해 사용자에게 "보정 제안".
# DART 원본 파일명 패턴 예: "[신한지주]분기보고서(2025.11.14).pdf",
#   "[KB금융]반기연결검토보고서(2025.08.14).pdf". 자동 적용은 안 하고 제안만.
# 괄호 안 날짜는 '접수월' → 보고서유형 키워드를 1차, 월을 분기 보조판정에만 사용.
_DETECT_DATE_RE = re.compile(r"\((\d{4})(?:[.\-/](\d{1,2}))?(?:[.\-/]\d{1,2})?\)")
# 결산기준일 "YYYY년 M월 DD일" — 본문 내용에서 분기/연간을 가장 신뢰성 있게 식별.
_STMT_DATE_RE = re.compile(r"(\d{4})\s*년\s*(3|6|9|12)\s*월\s*(?:30|31)\s*일")


def detect_company_from_text(text: str) -> Optional[str]:
    """텍스트(파일명/본문)에서 내부 회사표기(신한/KB/하나/우리) 추정. 구체어 우선."""
    t = text or ""
    # KB 모호성 회피 위해 '금융' 포함 변형을 먼저 검사(부분일치).
    for needle, internal in (("신한", "신한"), ("하나금융", "하나"), ("하나", "하나"),
                             ("우리금융", "우리"), ("우리", "우리"),
                             ("KB금융", "KB"), ("KB", "KB")):
        if needle in t:
            return internal
    return None


def detect_doc_type_from_text(text: str) -> Optional[str]:
    """텍스트에서 문서유형 추정. collect_dart._filename_matches_doc_type 규칙 미러+확장.

    - review      : 연결검토(또는 연결+검토/감사) → 연결재무제표.
    - review_sep  : 검토/감사 & 연결 미포함 → 별도재무제표.
    - report      : 사업/분기/반기보고서 & 검토·감사 미포함 → 본문 보고서(연결·별도 중립).
    """
    t = text or ""
    if "연결검토" in t:
        return "review"
    # 사업/분기/반기보고서는 검토·감사보고서와 명확히 구분 — 검토/감사 키워드가 없을 때만 report.
    if (("사업보고서" in t or "분기보고서" in t or "반기보고서" in t)
            and "검토" not in t and "감사" not in t):
        return "report"
    if ("검토" in t or "감사" in t) and "연결" not in t:
        return "review_sep"
    if "연결" in t and ("검토" in t or "감사" in t):  # 연결감사보고서
        return "review"
    return None


def detect_period_from_text(text: str) -> Optional[str]:
    """텍스트(파일명/본문)에서 기간(YYYY + Q1/Q2/Q3/FY) 추정. 없으면 None.

    우선순위:
      1) 결산기준일 "YYYY년 M월 DD일"(본문) — 3월=Q1·6월=Q2·9월=Q3·12월=FY(연간확정). 가장 신뢰.
      2) 명시적 분기/반기 표기(1분기/반기·2분기/3분기) + 파일명 결산기 마커 월.
      3) 분기인데 회차 불명 → 월 보조판정, 그래도 불명이면 보수적 Q3.
    연간 감사·사업보고서(분기/반기 표기 없음)는 본문 결산일로 FY 로 식별된다.
    """
    t = text or ""
    # 1) 결산기준일(본문) — 분기/연간을 직접 식별(파일명의 접수일과 혼동 없음)
    sm = _STMT_DATE_RE.search(t)
    if sm:
        q = {"3": "Q1", "6": "Q2", "9": "Q3", "12": "FY"}[sm.group(2)]
        return f"{sm.group(1)}{q}"
    # 2) 분기/반기 키워드 + 결산기 마커 월
    m = _DETECT_DATE_RE.search(t)
    year = m.group(1) if m else None
    mon = int(m.group(2)) if (m and m.group(2)) else None
    suffix = None
    if "반기" in t or "2분기" in t or "제2분기" in t:   # 반기=2분기
        suffix = "Q2"
    elif "1분기" in t or "제1분기" in t:
        suffix = "Q1"
    elif "3분기" in t or "제3분기" in t:
        suffix = "Q3"
    elif "분기" in t:                       # 회차 불명 → 월 보조판정
        if mon is not None and 3 <= mon <= 6:
            suffix = "Q1"
        elif mon is not None and (mon >= 10 or mon <= 2):
            suffix = "Q3"
        else:
            suffix = "Q3"
    if year and suffix:
        return f"{year}{suffix}"
    return None


def detect_pdf_meta_from(filename: str, page_text: str = "") -> dict:
    """파일명 우선 → 부족분만 1페이지 텍스트로 폴백 감지. 항상 dict 반환."""
    name = filename or ""
    company = detect_company_from_text(name)
    period = detect_period_from_text(name)
    doc_type = detect_doc_type_from_text(name)
    source = "filename"
    if page_text and not (company and period and doc_type):
        company = company or detect_company_from_text(page_text)
        period = period or detect_period_from_text(page_text)
        doc_type = doc_type or detect_doc_type_from_text(page_text)
        if company or period or doc_type:
            source = "filename+text"
    return {"company": company, "period": period, "doc_type": doc_type, "source": source}


@app.post("/api/library/upload")
async def upload_to_library(
    file: UploadFile = File(...),
    company: str = Form(...),
    period: str = Form(...),
    doc_type: str = Form("review"),
):
    """단일 (회사, 기간) PDF를 라이브러리에 업로드. doc_type 으로 review/review_sep 구분."""
    dt = validate_doc_type(doc_type)
    target_dir = entry_dir(company, period)
    target_dir.mkdir(parents=True, exist_ok=True)

    target_pdf = pdf_path(company, period, dt)
    overwriting = target_pdf.exists()
    content = await file.read()
    target_pdf.write_bytes(content)

    # 원본 파일명으로도 같은 폴더에 1부 보존(표시·다운로드용). 작업본은 review.pdf 유지.
    original_stored = safe_original_filename(file.filename)
    if original_stored:
        (target_dir / original_stored).write_bytes(content)

    with fitz.open(target_pdf) as doc:
        page_count = doc.page_count

    # 카탈로그 1행/(회사,기간) 유지. 한 문서 업로드가 다른 문서 행을 지우지 않도록 머지.
    existing = next((e for e in load_catalog()["entries"]
                     if e["company"] == company and e["period"] == period), {})
    existing = {k: v for k, v in existing.items() if k not in ("company", "period")}

    if dt == "review_sep":
        # review_sep 업로드 → review_sep_* 필드만 갱신, review 필드 보존.
        fields = {
            **existing,
            "review_sep_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "review_sep_filename_original": file.filename,
            "review_sep_pages": page_count,
            "review_sep_size_mb": round(len(content) / (1024 * 1024), 2),
            "review_sep_indexed": False,
            "review_sep_notes_count": 0,
            "review_sep_detected_unit": None,
        }
    elif dt == "report":
        # report 업로드 → report_* 필드만 갱신, review/review_sep 보존.
        fields = {
            **existing,
            "report_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "report_filename_original": file.filename,
            "report_pages": page_count,
            "report_size_mb": round(len(content) / (1024 * 1024), 2),
            "report_indexed": False,
            "report_notes_count": 0,
            "report_detected_unit": None,
        }
    else:
        # review 업로드 → review_* 필드만 갱신, review_sep_* 보존.
        fields = {
            **existing,
            "review_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "review_filename_original": file.filename,
            "review_pages": page_count,
            "review_size_mb": round(len(content) / (1024 * 1024), 2),
            "review_indexed": False,
            "review_notes_count": 0,
            "review_detected_unit": None,
        }
    upsert_catalog_entry(company, period, **fields)

    return {
        "company": company,
        "period": period,
        "doc_type": dt,
        "pages": page_count,
        "indexed": False,
        "warning": "기존 항목을 덮어썼습니다." if overwriting else None,
    }


@app.post("/api/library/detect")
async def detect_pdf_meta(file: UploadFile = File(...)):
    """업로드 전 PDF 자동 감지 — 회사/기간/문서유형 추정값 반환(보정 제안용).

    디스크에 쓰지 않고 카탈로그도 건드리지 않는다(순수 조회). 파일명으로 부족하면
    1페이지 텍스트를 메모리에서 읽어 폴백. 감지 실패 항목은 null.
    """
    name = file.filename or ""
    meta = detect_pdf_meta_from(name)
    if not (meta["company"] and meta["period"] and meta["doc_type"]):
        try:
            content = await file.read()
            with fitz.open(stream=content, filetype="pdf") as doc:
                # 결산기준일(YYYY년 M월 DD일)은 표지 다음 페이지에 있을 수 있어 앞 5p 스캔.
                head = "".join(doc[i].get_text() for i in range(min(5, doc.page_count)))
            meta = detect_pdf_meta_from(name, head)
        except Exception:
            pass  # 파싱 실패 → 파일명 기반 결과만 반환
    return meta


@app.get("/api/library")
async def get_library():
    """매트릭스 형태로 라이브러리 카탈로그 반환."""
    cat = load_catalog()
    matrix: Dict[str, List[str]] = {c: [] for c in VALID_COMPANIES}
    periods_seen: set = set()
    indexed_count = 0

    for e in cat["entries"]:
        matrix.setdefault(e["company"], []).append(e["period"])
        periods_seen.add(e["period"])
        if e.get("indexed"):
            indexed_count += 1

    for c in matrix:
        matrix[c] = sorted(set(matrix[c]), key=period_sort_key)

    # 각 엔트리에 문서유형별 원본 PDF 파일명 노출(PDF 뷰어 표시·다운로드용). 없으면 None.
    # 카탈로그 원본은 변형하지 않고 표시용 사본에만 주입(부수효과 차단).
    enriched = []
    for e in cat["entries"]:
        company, period = e["company"], e["period"]
        enriched.append({
            **e,
            "review_filename_original": original_pdf_name(company, period, "review"),
            "review_sep_filename_original": original_pdf_name(company, period, "review_sep"),
            "report_filename_original": original_pdf_name(company, period, "report"),
        })

    return {
        "matrix": matrix,
        "available_periods": sorted(periods_seen, key=period_sort_key),
        "total_files": len(cat["entries"]),
        "total_indexed": indexed_count,
        "entries": enriched,
    }


# ----------------------------------------------------------------------------
# 검색개선 Phase5b — 인덱스 이식(내보내기/가져오기/rescan). GPU 없는 PC 재활용.
# 셀 폴더는 절대경로 없는 자기완결 구조(실측) — zip 반출입 + 카탈로그 재구성으로 이식.
# ----------------------------------------------------------------------------
_EXPORT_EXCLUDE_DIRS = {"source"}  # 검색·표시에 불필요한 원본 보존물(용량 절감)


def _cell_entry_from_disk(company: str, period: str) -> Optional[dict]:
    """디스크 실파일 기준으로 카탈로그 엔트리 1건 재구성(가져오기/rescan 공용).

    수집 플래그·인덱싱 카운트를 PDF/인덱스/fs_structured 존재와 인덱스 내용으로 도출.
    셀에 의미 있는 데이터가 없으면 None(빈 디렉터리는 등록하지 않음).
    """
    d = entry_dir(company, period)
    if not d.exists():
        return None
    entry: Dict[str, Any] = {}
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            entry["rcept_no"] = meta.get("rcept_no")
            entry["report_nm"] = meta.get("report_nm")
        except (OSError, json.JSONDecodeError):
            pass  # meta 손상 — 파일 기준 플래그만으로 진행
    entry["review_collected"] = pdf_path(company, period, "review").exists()
    entry["review_sep_collected"] = pdf_path(company, period, "review_sep").exists()
    entry["report_collected"] = pdf_path(company, period, "report").exists()
    entry["fs_collected"] = (d / "fs_structured.json").exists()

    has_any = any(entry.get(k) for k in ("review_collected", "review_sep_collected",
                                         "report_collected", "fs_collected"))
    for dt, prefix in (("review", "review_"), ("review_sep", "review_sep_"),
                       ("report", "report_")):
        ip = index_path(company, period, dt)
        if not ip.exists():
            continue
        try:
            idx = json.loads(ip.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        notes = idx.get("notes", [])
        n_conn = sum(1 for n in notes if n.get("fs_div") == "연결")
        n_sep = sum(1 for n in notes if n.get("fs_div") == "별도")
        entry[f"{prefix}indexed" if prefix else "indexed"] = True
        entry[f"{prefix}notes_count"] = len(notes)
        entry[f"{prefix}notes_count_연결"] = n_conn
        entry[f"{prefix}notes_count_별도"] = n_sep
        entry[f"{prefix}source_type"] = idx.get("source_type")
        entry[f"{prefix}detected_unit"] = idx.get("detected_unit")
        has_any = True
    return entry if has_any else None


@app.post("/api/library/rescan")
async def rescan_library():
    """디스크 스캔으로 카탈로그 재구성 — 폴더 복사/수동 작업 후 매트릭스 복원용."""
    old = {(e["company"], e["period"]): e for e in load_catalog()["entries"]}
    entries = []
    for cell_dir in sorted(LIBRARY_ROOT.glob("*/*")):
        company, period = cell_dir.parent.name, cell_dir.name
        try:
            validate_company(company)
            validate_period(period)
        except HTTPException:
            continue  # 규격 외 디렉터리 무시
        e = _cell_entry_from_disk(company, period)
        if e is None:
            continue
        prev = old.get((company, period), {})
        if prev.get("collected_at"):
            e["collected_at"] = prev["collected_at"]
        entries.append({"company": company, "period": period, **e})
    save_catalog({"entries": entries})
    return {"cells": len(entries)}


@app.get("/api/library/export")
async def export_library(cells: Optional[str] = None, include_raw: bool = False):
    """라이브러리 셀들을 이식용 zip 으로 내보내기(+manifest — 가져오기 검증용).

    include_raw=False(기본): source/ 원본 보존물 제외 — 검색·뷰어 동작에 불필요,
    반입 용량 대폭 절감. 검색 인덱스·PDF·재무데이터·meta 는 포함.
    """
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask

    if cells:
        targets = []
        for c in cells.split(","):
            comp, _, per = c.strip().partition("/")
            targets.append((validate_company(comp), validate_period(per)))
    else:
        targets = [(d.parent.name, d.name) for d in sorted(LIBRARY_ROOT.glob("*/*"))
                   if d.is_dir()]
    manifest = {
        "app": "prism-fs",
        "schema": INDEX_SCHEMA,
        "embed_dim": EMBED_DIM,
        "embed_backend": "local-model" if USE_LOCAL_EMBED else "bigram",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "include_raw": include_raw,
        "cells": [f"{c}/{p}" for c, p in targets],
    }
    tmp = tempfile.NamedTemporaryFile(prefix="prism_export_", suffix=".zip",
                                      delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for comp, per in targets:
            d = entry_dir(comp, per)
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(LIBRARY_ROOT)
                if not include_raw and _EXPORT_EXCLUDE_DIRS & set(rel.parts):
                    continue
                zf.write(f, str(rel).replace("\\", "/"))
    fname = f"prism_library_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    return FileResponse(tmp.name, filename=fname, media_type="application/zip",
                        background=BackgroundTask(os.unlink, tmp.name))


def _validate_zip_member(name: str) -> Optional[tuple]:
    """zip 멤버 경로 검증 — (company, period) 반환, 셀 파일이 아니거나 위험하면 None.

    경로 탈출(..·절대경로·드라이브) 차단(보안 규칙). manifest.json 등 루트 파일은 None.
    """
    norm = name.replace("\\", "/")
    if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
        return None
    parts = norm.split("/")
    if len(parts) < 3:
        return None  # 루트 파일(manifest 등) — 셀 콘텐츠 아님
    company, period = parts[0], parts[1]
    if company not in VALID_COMPANIES or not PERIOD_PATTERN.match(period):
        return None
    return company, period


def _import_zip_blocking(zip_file: Path, overwrite: bool) -> dict:
    """이식 zip 반입 코어(동기) — manifest 검증 + 셀 단위 병합 + 카탈로그 등록."""
    import zipfile
    with zipfile.ZipFile(zip_file) as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise HTTPException(400, "manifest.json 이 없는 zip — prism-fs 내보내기 파일이 아닙니다.")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        dim = manifest.get("embed_dim")
        if dim and dim != EMBED_DIM:
            raise HTTPException(400, (
                f"임베딩 차원 불일치 — 반입 인덱스 {dim} ↔ 현재 백엔드 {EMBED_DIM}. "
                "동일 임베딩 모델(ko-sroberta 동봉 exe) 환경에서 가져오세요."))

        by_cell: Dict[tuple, list] = {}
        for n in names:
            cell = _validate_zip_member(n)
            if cell and not n.endswith("/"):
                by_cell.setdefault(cell, []).append(n)

        imported, skipped = [], []
        for (company, period), members in sorted(by_cell.items()):
            dest = entry_dir(company, period)
            if dest.exists() and any(dest.iterdir()) and not overwrite:
                skipped.append(f"{company}/{period}")
                continue
            for m in members:
                target = LIBRARY_ROOT / Path(m.replace("/", os.sep))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            e = _cell_entry_from_disk(company, period)
            if e:
                upsert_catalog_entry(company, period, **e)
            imported.append(f"{company}/{period}")
        return {"imported": imported, "skipped": skipped,
                "manifest_schema": manifest.get("schema")}


@app.post("/api/library/import")
async def import_library(file: UploadFile = File(...),
                         overwrite: bool = Form(False)):
    """이식 zip 가져오기 — 차원/스키마/경로 검증 후 셀 병합 + 카탈로그 자동 등록."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix="prism_import_", suffix=".zip",
                                      delete=False)
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        return await asyncio.to_thread(_import_zip_blocking, Path(tmp.name), overwrite)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# 주의: 파라미터 라우트(/{company}/{period})보다 먼저 선언해야 함.
# FastAPI 는 선언 순서로 매칭하므로, 뒤에 두면 index/status 가 company=index 로 잘못 매칭됨.
@app.get("/api/library/index/status")
async def get_index_status():
    """전체 라이브러리의 인덱싱 상태."""
    return {"items": [{"key": k, **v} for k, v in INDEX_STATUS.items()]}


# 주의: 아래 정적 경로는 반드시 "/{company}/{period}" 파라미터 라우트보다 먼저
# 선언해야 한다(안 그러면 company="collect"/period="status" 로 오매칭).
@app.get("/api/library/collect/status")
async def get_collect_status():
    """전체 라이브러리의 DART 수집 상태."""
    return {"items": [{"key": k, **v} for k, v in COLLECT_STATUS.items()]}


@app.get("/api/library/{company}/{period}")
async def get_library_entry(company: str, period: str):
    validate_company(company)
    validate_period(period)
    cat = load_catalog()
    entry = next(
        (e for e in cat["entries"]
         if e["company"] == company and e["period"] == period),
        None,
    )
    if not entry:
        raise HTTPException(404, "항목을 찾을 수 없습니다.")
    return entry


@app.delete("/api/library/{company}/{period}")
async def delete_library_entry(company: str, period: str):
    d = entry_dir(company, period)
    if d.exists():
        # 하위 디렉터리(collect_dart 의 source/)도 있을 수 있어 rmtree 로 삭제.
        shutil.rmtree(d)
    remove_catalog_entry(company, period)
    # INDEX_STATUS 키가 "{company}/{period}/{doc_type}" 이므로 두 문서유형 모두 정리.
    for dt in VALID_DOC_TYPES:
        INDEX_STATUS.pop(f"{company}/{period}/{dt}", None)
    return {"deleted": True, "company": company, "period": period}


@app.delete("/api/library/{company}/{period}/doc/{doc_type}")
async def delete_library_doc(company: str, period: str, doc_type: str):
    """문서유형별 단건 삭제 — 해당 PDF·인덱스·원본 사본만 제거, 나머지 문서/재무데이터 보존.

    남은 작업본 PDF 가 0개이고 재무데이터(fs_structured.json)도 없으면 셀 전체 제거.
    경로 세그먼트가 4개(.../doc/{doc_type})라 3-세그먼트 셀 삭제 라우트와 충돌하지 않는다.
    """
    validate_company(company)
    validate_period(period)
    dt = validate_doc_type(doc_type)
    d = entry_dir(company, period)
    if not d.exists():
        raise HTTPException(404, "항목을 찾을 수 없습니다.")

    # 작업본 PDF·인덱스·원본 사본 삭제(표준 작업본명은 원본 사본 삭제에서 제외).
    wp = pdf_path(company, period, dt)
    if wp.exists():
        wp.unlink()
    ip = index_path(company, period, dt)
    if ip.exists():
        ip.unlink()
    orig = original_pdf_name(company, period, dt)
    if orig and orig.lower() not in _STANDARD_PDF_NAMES:
        op = d / orig
        if op.exists():
            op.unlink()

    # 카탈로그에서 해당 doc_type 필드만 제거(다른 문서 보존).
    cat = load_catalog()
    entry = next((e for e in cat["entries"]
                  if e["company"] == company and e["period"] == period), None)
    if entry is not None:
        cleaned = _strip_doc_fields(entry, dt)
        fields = {k: v for k, v in cleaned.items() if k not in ("company", "period")}
        upsert_catalog_entry(company, period, **fields)
    INDEX_STATUS.pop(f"{company}/{period}/{dt}", None)

    # 남은 PDF 0개 + 재무데이터 없음 → 셀 전체 제거.
    fs_present = ((d / "fs_structured.json").exists()
                 or bool((entry or {}).get("fs_collected")))
    any_pdf_left = any(pdf_path(company, period, x).exists() for x in VALID_DOC_TYPES)
    if not any_pdf_left and not fs_present:
        shutil.rmtree(d, ignore_errors=True)
        remove_catalog_entry(company, period)
        for x in VALID_DOC_TYPES:
            INDEX_STATUS.pop(f"{company}/{period}/{x}", None)
        return {"deleted": True, "scope": "cell", "company": company, "period": period}
    return {"deleted": True, "scope": dt, "company": company, "period": period}


# ----------------------------------------------------------------------------
# 2) Indexing — 주석 구조 추출 + 임베딩
# ----------------------------------------------------------------------------
NOTE_HEADER_PATTERN = re.compile(
    r"^\s*(\d{1,2})\.\s+([가-힣A-Za-z][^\n]{2,60})",
    re.MULTILINE,
)
NOTES_SECTION_KEYWORDS = ["주석", "Notes to", "재무제표에 대한 주석"]
UNIT_PATTERN = re.compile(r"(?:단위[:\s]*)?(백만원|억원|천원|원|KRW)")

# 주석 시퀀스 탐지 상수
# - GAP_TOLERANCE: 헤더 일부가 추출되지 않아 번호가 건너뛰어도 같은 시퀀스로 인정할 최대 간격
#   (예: 신한 14→16 처럼 표/페이지 레이아웃 때문에 일부 헤더가 누락되는 경우 흡수)
# - MIN_VALID_MAX_NO: 진짜 주석 시퀀스로 인정할 도달 최대 번호 하한.
#   재무제표 표의 번호행 런은 max_no 가 작아(<15) 자연히 탈락.
GAP_TOLERANCE = 4
MIN_VALID_MAX_NO = 15

# 제목 끝의 주석참조 꼬리 (예: "...관련 손익(주석12)") 제거용
_NOTE_REF_TAIL = re.compile(r"\s*\(주석\s*\d+\)\s*$")

# ── Step 5: 문서유형 인지 추출 ──────────────────────────────────────────────
# 전체 분기/사업보고서는 진짜 주석 제목에 (연결)/(별도) 접미가 일관 존재한다.
# 슬림 검토보고서는 접미가 전혀 없다(문서 전체가 연결 주석).
#   _FS_DIV_SUFFIX: 제목 끝의 연결/별도 접미 (그룹 분리·fs_div 태깅·제목에서 제거).
#   _TOC_LEADER:    목차 점선 leader(`...` 3개↑ 또는 `…`) — 후보·제목에서 제거.
#   FULL_REPORT_SUFFIX_MIN: 이 수 이상 접미 헤더가 있으면 full_report 모드로 판정.
_FS_DIV_SUFFIX = re.compile(r"\s*\((연결|별도)\)\s*$")
_TOC_LEADER = re.compile(r"\.{3,}|…")
FULL_REPORT_SUFFIX_MIN = 5
# full_report 접미 그룹 전용 gap 허용치. 접미로 이미 진짜 주석만 남았으므로(비주석 흡수 위험 없음)
# 슬림(4)보다 크게 잡아 표/페이지 레이아웃으로 일부 헤더가 누락된 구간을 흡수
# (예: 사업보고서 연결 주석 no22→no27 의 +5 단절). 슬림 경로는 영향 없음(기본값 유지).
FULL_REPORT_GAP_TOLERANCE = 7


def _collect_header_candidates(doc) -> List[Dict[str, Any]]:
    """전체 페이지에서 `숫자. 제목` 형태 헤더 후보를 페이지 순서대로 수집.

    각 후보에 fs_div(연결/별도/None) 태그를 부착한다(접미 인지). 목차(점선 leader)
    라인은 후보에서 제외한다 — 전체보고서 목차의 `N.제목......페이지` 가 흡수되는 것을 차단.
    """
    candidates: List[Dict[str, Any]] = []
    for pno in range(doc.page_count):
        text = doc[pno].get_text()
        for m in NOTE_HEADER_PATTERN.finditer(text):
            note_no = int(m.group(1))
            title = m.group(2).strip()
            # 제목이 너무 짧거나 숫자로 시작하면(표 셀 잔재) 제외
            if len(title) < 3 or re.match(r"^\d", title):
                continue
            # 목차 라인(점선 leader 포함) 제외
            if _TOC_LEADER.search(title):
                continue
            sm = _FS_DIV_SUFFIX.search(title)
            fs_div = sm.group(1) if sm else None
            candidates.append({"page": pno + 1, "no": note_no,
                               "title": title, "fs_div": fs_div})
    return candidates


def _simulate_monotonic_run(candidates: List[Dict[str, Any]], start_idx: int,
                            gap_tolerance: int = GAP_TOLERANCE):
    """start_idx 후보에서 출발해 엄격 증가(gap 허용) 런을 시뮬레이션.

    채택 규칙: 다음 후보 번호가 직전 채택 번호보다 크고 (직전 + gap_tolerance) 이하면 채택.
    번호가 같거나 작으면(하위표 리셋) 건너뜀.

    gap_tolerance: 슬림은 기본값(GAP_TOLERANCE). full_report 접미 그룹은 후보 전체가
    이미 (연결)/(별도) 접미로 검증된 진짜 주석이라 더 큰 값을 허용(헤더 일부 미추출 흡수).

    Returns:
        (멤버 인덱스 리스트, 도달 최대 번호, 걸친 distinct 페이지 수)
    """
    last_no = candidates[start_idx]["no"]
    members = [start_idx]
    for j in range(start_idx + 1, len(candidates)):
        no = candidates[j]["no"]
        if last_no < no <= last_no + gap_tolerance:
            members.append(j)
            last_no = no
    pages = {candidates[k]["page"] for k in members}
    return members, last_no, len(pages)


def _select_note_sequence(candidates: List[Dict[str, Any]],
                          gap_tolerance: int = GAP_TOLERANCE):
    """no==1 시작 후보들 중 도달 최대 번호가 가장 큰 단조 런을 진짜 주석 시퀀스로 선택.

    동률 시 페이지 span 이 큰 쪽 우선. 유효 시퀀스가 없으면(max_no < MIN_VALID_MAX_NO) None 반환.
    """
    start_indices = [i for i, c in enumerate(candidates) if c["no"] == 1]
    best = None  # (max_no, page_span, members)
    for si in start_indices:
        members, max_no, page_span = _simulate_monotonic_run(candidates, si, gap_tolerance)
        key = (max_no, page_span)
        if best is None or key > (best[0], best[1]):
            best = (max_no, page_span, members)

    if best is None or best[0] < MIN_VALID_MAX_NO:
        return None
    return [candidates[k] for k in best[2]]


def _clean_title(title: str) -> str:
    """제목 정리 — 끝의 ` :`/`:`, `(주석N)` 참조 꼬리, `(연결)`/`(별도)` 접미,
    목차 점선 leader 꼬리 제거. 번역·요약·치환 없음(원문 발췌)."""
    title = _TOC_LEADER.split(title)[0]      # 점선 leader 이후(목차 페이지번호 등) 제거
    title = _FS_DIV_SUFFIX.sub("", title)    # 연결/별도 접미 제거(fs_div 로 별도 보존)
    title = _NOTE_REF_TAIL.sub("", title)
    title = title.rstrip()
    title = re.sub(r"\s*:\s*$", "", title)  # KB 처럼 헤더 끝에 붙는 콜론 제거
    return title.strip()


def _detect_unit(doc, scan_start_page: int) -> Optional[str]:
    """주석 시작 페이지부터 단위 표기를 카운트해 최빈값 반환. 환산 없이 탐지만."""
    unit_counter: Dict[str, int] = {}
    start = max(scan_start_page - 1, 0)  # page 번호(1-base) → 인덱스(0-base)
    for pno in range(start, doc.page_count):
        for m in UNIT_PATTERN.finditer(doc[pno].get_text()):
            unit_counter[m.group(1)] = unit_counter.get(m.group(1), 0) + 1
    return max(unit_counter, key=unit_counter.get) if unit_counter else None


def _build_notes_from_sequence(members: List[Dict[str, Any]], total_pages: int,
                               fs_div: Optional[str] = None):
    """선택된 시퀀스 멤버를 반환 형식의 notes 로 변환. page_end = 다음 주석 시작 - 1.

    fs_div: 그룹 전체에 강제할 연결/별도 값(full_report). None 이면 후보의 개별 fs_div 사용.
    """
    notes = []
    for i, c in enumerate(members):
        page_end = members[i + 1]["page"] - 1 if i + 1 < len(members) else total_pages
        notes.append({
            "no": c["no"],
            "title": _clean_title(c["title"]),
            "page_start": c["page"],
            "page_end": max(page_end, c["page"]),
            "fs_div": fs_div if fs_div is not None else c.get("fs_div"),
        })
    return notes


def _fallback_keyword_notes(doc):
    """비표준 문서 폴백 — 기존 방식(첫 '주석' 키워드 페이지부터 헤더 수집)."""
    notes_start_page = 0
    for pno in range(doc.page_count):
        if any(kw in doc[pno].get_text() for kw in NOTES_SECTION_KEYWORDS):
            notes_start_page = pno
            break

    candidates = []
    seen = set()
    for pno in range(notes_start_page, doc.page_count):
        for m in NOTE_HEADER_PATTERN.finditer(doc[pno].get_text()):
            note_no = int(m.group(1))
            title = m.group(2).strip()
            if len(title) < 3 or re.match(r"^\d", title):
                continue
            key = (note_no, pno + 1)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"page": pno + 1, "no": note_no, "title": title})

    return _build_notes_from_sequence(candidates, doc.page_count), notes_start_page + 1


# 별도(개별)재무제표 주석 섹션 헤더 — 분기보고서에서 연결 주석 뒤에 위치.
# "(별도)" 접미가 없는 회사가 있어(예: 신한·우리) 섹션 헤더로 별도 영역을 앵커링한다.
_SEPARATE_SECTION_RE = re.compile(r"(별도재무제표|재무제표에 대한 주석|재무제표\s*주석)")


def _find_separate_section_page(doc, after_page: int) -> Optional[int]:
    """연결 주석 영역(after_page) 이후 첫 '별도재무제표/재무제표 주석' 섹션 페이지(1-base).

    after_page 이후부터 스캔하므로 앞쪽 '연결재무제표 주석' 은 자연히 제외된다.
    """
    for pno in range(max(after_page, 0), doc.page_count):
        if _SEPARATE_SECTION_RE.search(doc[pno].get_text()):
            return pno + 1
    return None


def _extract_full_report_notes(candidates: List[Dict[str, Any]], total_pages: int, doc=None):
    """full_report 추출 — 연결은 (연결) 접미 앵커, 별도는 접미 또는 섹션 앵커.

    1) 연결: (연결) 접미 후보 그룹 → 단조 런.
    2) 별도: (별도) 접미 그룹이 있으면 그것으로, 없으면(접미 미사용 회사)
       연결 영역 이후 '재무제표 주석' 섹션 페이지부터의 무접미 후보로 단조 런(1.. 시작).
    각 그룹 fs_div 를 note 에 강제 태깅. 연결+별도 합쳐 반환.

    Returns: (notes, scan_start) — scan_start 는 단위 탐지 시작 페이지.
    """
    notes: List[Dict[str, Any]] = []
    scan_starts: List[int] = []

    # 1) 연결 (접미 앵커)
    conn_last_page = 0
    conn_group = [c for c in candidates if c.get("fs_div") == "연결"]
    if conn_group:
        seq = _select_note_sequence(conn_group, gap_tolerance=FULL_REPORT_GAP_TOLERANCE)
        if seq is not None:
            notes.extend(_build_notes_from_sequence(seq, total_pages, fs_div="연결"))
            scan_starts.append(seq[0]["page"])
            conn_last_page = max(c["page"] for c in seq)

    # 2) 별도 — 접미 그룹 우선, 없으면 섹션 앵커 폴백(접미 미사용 회사)
    sep_group = [c for c in candidates if c.get("fs_div") == "별도"]
    sep_seq = _select_note_sequence(sep_group, gap_tolerance=FULL_REPORT_GAP_TOLERANCE) if sep_group else None
    if sep_seq is None and doc is not None and conn_last_page:
        sep_start = _find_separate_section_page(doc, conn_last_page)
        if sep_start:
            sep_cands = [c for c in candidates
                         if c.get("fs_div") is None and c["page"] >= sep_start]
            sep_seq = _select_note_sequence(sep_cands, gap_tolerance=FULL_REPORT_GAP_TOLERANCE)
    if sep_seq is not None:
        notes.extend(_build_notes_from_sequence(sep_seq, total_pages, fs_div="별도"))
        scan_starts.append(sep_seq[0]["page"])

    scan_start = min(scan_starts) if scan_starts else 1
    return notes, scan_start


# Phase D: 노트 본문-존재 최소 글자수(이 미만이면 헤더 오탐으로 보고 드롭). 보수적 하한.
MIN_NOTE_BODY_CHARS = 30
# 본문/단위 스캔 시 노트당 페이지 상한(마지막 노트가 문서 끝까지 걸쳐도 폭주 방지).
NOTE_SCAN_PAGE_CAP = 8


def _note_body_text(doc, page_start: int, page_end: int, cap_pages: int = NOTE_SCAN_PAGE_CAP) -> str:
    """노트 페이지 범위의 본문 텍스트(캡 적용). 추출 검증·단위 탐지용(가공 없음)."""
    if not page_start:
        return ""
    last = min(page_start + cap_pages - 1, page_end or page_start, doc.page_count)
    out = []
    for p in range(page_start, last + 1):
        if 1 <= p <= doc.page_count:
            out.append(doc[p - 1].get_text())
    return "\n".join(out)


def _annotate_notes(doc, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """노트별 본문 존재 검증(헤더 오탐 드롭) + 노트별 단위 태깅. 텍스트 전용·환산 없음.

    - 제목 헤더만 잡히고 본문이 사실상 없는 후보(목차 잔재·표 셀 등) 제거.
    - 노트 페이지 범위에서 최빈 단위 표기를 note['detected_unit'] 로 보존(문서 1개 단위의 한계 보완).
    """
    kept = []
    for n in notes:
        body = _note_body_text(doc, n.get("page_start"), n.get("page_end"))
        body_chars = len(re.sub(r"\s", "", body))
        if body_chars < MIN_NOTE_BODY_CHARS:
            continue  # 본문 없는 헤더 오탐 — 드롭
        unit_counter: Dict[str, int] = {}
        for m in UNIT_PATTERN.finditer(body):
            unit_counter[m.group(1)] = unit_counter.get(m.group(1), 0) + 1
        n["detected_unit"] = max(unit_counter, key=unit_counter.get) if unit_counter else None
        kept.append(n)
    return kept


# Phase A: 본문 청크 파라미터. 인덱스 비대 방지 위해 노트당 청크·페이지 상한.
# 검색개선 Phase2: 청크 450→250자 — 임베딩 모델(ko-sroberta) max_seq_length=128토큰에
# 정합(450자는 모델이 뒷부분 ~절반을 무음 절단했음). 페이지 스캔 캡 제거 + 노트당
# 상한 완화로 장문 주석(금융상품 위험 등 20p+) 전 범위 커버. 용량은 벡터 반올림으로 상쇄.
CHUNK_CHARS = 250          # 청크 목표 길이(자)
CHUNK_OVERLAP = 50         # 청크 간 겹침(경계 문맥 보존)
MAX_CHUNKS_PER_NOTE = 120  # 노트당 청크 상한(인덱스 용량 통제, 40→120)
MAX_CHUNKS_PER_PAGE = 4    # 페이지당 청크 상한 — 긴 노트가 앞 페이지에서 예산을 소진하지 않고
                           # 전 페이지 범위에 고르게 분산되도록(긴 주석 커버리지↑)
INDEX_SCHEMA = 4           # 인덱싱 스키마 버전(4: 문장경계 청크. 3=raw 250자 슬라이스. 구버전 호환 읽기)
EMB_ROUND = 5              # 인덱스 저장 벡터 소수점 자릿수 — JSON 크기 ~45%↓, 코사인 오차 <1e-4

# 문장 경계: 종결부호(. 。 ! ?) 뒤 공백 또는 줄바꿈. 한국어 주석은 줄바꿈도 의미 단위.
_SENT_SPLIT_RE = re.compile(r"(?<=[.。!?])\s+|\n+")


def _split_sentences(text: str) -> List[str]:
    """공백 정규화 후 문장 단위로 분리(빈 조각 제외). 가공·요약 없음."""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """본문을 문장 경계 기준으로 size(자) 한도까지 그리디 패킹.

    문장 중간 절단을 피해 완결된 의미 단위로 임베딩·스니펫 품질을 높인다(schema 4).
    종결부호 없는 초장문 문장(>size, 표 평문 등)만 문자 슬라이딩(overlap)으로 폴백.
    공백 정규화 외 가공·요약 없음.
    """
    sents = _split_sentences(text)
    if not sents:
        return []
    chunks: List[str] = []
    cur = ""
    for s in sents:
        if len(s) > size:  # 단일 문장이 한도 초과 → 문자 슬라이싱 폴백
            if cur:
                chunks.append(cur)
                cur = ""
            step = max(size - overlap, 1)
            for i in range(0, len(s), step):
                chunks.append(s[i:i + size])
                if i + size >= len(s):
                    break
            continue
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= size:
            cur = f"{cur} {s}"
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _note_chunks(doc, page_start: int, page_end: int) -> List[Dict[str, Any]]:
    """노트 페이지 범위를 페이지-정확 청크로 분할(각 청크에 실제 page 보존 → 인용 정밀화).

    반환: [{text, page}] (임베딩·토큰은 index_entry 에서 부착). 노트당 상한 적용.
    Phase2: 페이지 스캔 캡(구 20p) 제거 — 노트 전 범위 스캔(장문 주석 뒷부분 누락 해소).
    """
    if not page_start:
        return []
    out: List[Dict[str, Any]] = []
    last = min(page_end or page_start, doc.page_count)
    for p in range(page_start, last + 1):
        if not (1 <= p <= doc.page_count):
            continue
        per_page = 0  # 페이지별 할당량 — 한 페이지가 노트 예산을 독식하지 못하게 분산
        for c in _chunk_text(doc[p - 1].get_text()):
            if len(c.strip()) < 20:  # 페이지 머리말·쪽번호 등 잔재 제외
                continue
            out.append({"text": c, "page": p})
            per_page += 1
            if len(out) >= MAX_CHUNKS_PER_NOTE:
                return out
            if per_page >= MAX_CHUNKS_PER_PAGE:
                break  # 이 페이지 할당량 소진 → 다음 페이지로(긴 노트 전 범위 커버)
    return out


_FULL_TEXT_CAP = 200_000  # 노트 전문 저장 상한(자) — 비정상 페이지 범위 방어


def _note_full_text(doc, page_start: int, page_end: int) -> str:
    """노트 페이지 범위의 본문 전문(공백 정규화) — RAG 컨텍스트·스니펫용. 임베딩 없음."""
    if not page_start:
        return ""
    last = min(page_end or page_start, doc.page_count)
    parts = []
    total = 0
    for p in range(page_start, last + 1):
        if not (1 <= p <= doc.page_count):
            continue
        t = re.sub(r"[ \t]+", " ", doc[p - 1].get_text() or "").strip()
        parts.append(t)
        total += len(t)
        if total >= _FULL_TEXT_CAP:
            break
    return "\n".join(parts)[:_FULL_TEXT_CAP]


def extract_notes_heuristic(pdf_path: Path, default_fs_div: str = "연결"):
    """PDF 에서 주석 헤더 시퀀스를 순수 휴리스틱(정규식+단조 런)으로 추출.

    문서유형 자동 판별:
    - (연결)/(별도) 접미 헤더가 임계(FULL_REPORT_SUFFIX_MIN) 이상 → full_report 모드
      (접미 앵커로 연결/별도 그룹 분리 추출). 그 외 → slim 모드(현행 단조런).

    default_fs_div: slim 모드에서 접미 미부착 노트에 태깅할 fs_div 기본값.
        검토보고서는 문서 단위로 연결/별도가 고정 → 호출부(index_entry)가 doc_type 으로 결정.
        review_sep 인덱싱 시 "별도", 그 외(review)는 "연결" 기본.
    AI/LLM 미사용. 반환 형식: (notes, detected_unit, source_type)
    - notes: [{"no","title","page_start","page_end","fs_div"}]
    - detected_unit: 탐지된 단위 표기(환산 없음) 또는 None
    - source_type: "full_report" | "slim"
    """
    doc = fitz.open(pdf_path)
    try:
        candidates = _collect_header_candidates(doc)
        suffix_count = sum(1 for c in candidates if c.get("fs_div"))

        if suffix_count >= FULL_REPORT_SUFFIX_MIN:
            # 전체 분기/사업보고서 — 접미 앵커로 연결/별도 분리 추출
            source_type = "full_report"
            notes, scan_start = _extract_full_report_notes(candidates, doc.page_count, doc)
        else:
            # 슬림 검토보고서 — 현행 단조런 유지
            source_type = "slim"
            sequence = _select_note_sequence(candidates)
            if sequence is not None:
                notes = _build_notes_from_sequence(sequence, doc.page_count)
                scan_start = sequence[0]["page"]
            else:
                # 비표준 문서 — 단조 시퀀스 탐지 실패 시 기존 키워드 방식으로 폴백
                notes, scan_start = _fallback_keyword_notes(doc)
            # 슬림 검토보고서는 문서 단위로 연결/별도 고정 → default_fs_div 로 태깅
            # (연결검토=review→"연결", 별도검토=review_sep→"별도"). 기본값 "연결" 유지 시 기존과 동일.
            for n in notes:
                if n.get("fs_div") is None:
                    n["fs_div"] = default_fs_div

        # Phase D: 본문-존재 검증(헤더 오탐 드롭) + 노트별 단위 태깅
        notes = _annotate_notes(doc, notes)
        detected_unit = _detect_unit(doc, scan_start)
        return notes, detected_unit, source_type
    finally:
        doc.close()


async def llm_refine_notes(candidates: list, sample_text: str) -> tuple:
    """Ollama LLM으로 1차 휴리스틱 결과 검증·보정. 실패 시 원본 그대로 반환.

    할루시네이션 방지 원칙:
    - 원본 텍스트에 없는 정보는 추가 금지
    - 확신 없으면 빈 응답 허용
    - JSON 스키마 강제, temperature=0
    """
    if not _OLLAMA_AVAILABLE:
        return candidates, "skipped"

    prompt = f"""당신은 한국 금융 감사보고서의 주석 구조를 검증하는 도구입니다.

엄격 규칙:
1. 제공된 원본 텍스트에서만 정보를 추출하세요. 외부 지식 사용 금지.
2. 원본에 명시되지 않은 정보는 절대 추가·추측하지 마세요.
3. 확신이 없는 항목은 결과에서 제외하세요. 추측은 오류보다 나쁩니다.
4. 숫자·금액·날짜는 생성하지 마세요. 주석 번호와 페이지만 허용.
5. 주석 제목은 원문 그대로 발췌하세요. 번역·요약·재구성 금지.

원본 텍스트 (일부):
---
{sample_text[:3000]}
---

휴리스틱으로 추출한 주석 후보:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

작업: 위 후보에서 명백히 잘못 추출된 항목(예: 표 데이터, 페이지 번호, 광고성 문구)을 제외하세요.
확신이 없는 항목도 제외하세요.

응답은 JSON 배열만 반환하세요. 다른 설명 텍스트 금지.
형식: [{{"no": 정수, "title": "제목", "page": 정수}}, ...]
"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 4000,
                        "top_p": 1.0,
                    },
                },
            )
            res.raise_for_status()
            text = res.json().get("response", "").strip()
            # JSON 배열 추출
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                return candidates, "no_json"
            refined = json.loads(m.group())
            # 검증: 원본 후보에 없는 제목은 제외 (할루시네이션 차단)
            original_titles = {c["title"] for c in candidates}
            verified = [r for r in refined
                        if isinstance(r, dict)
                        and "no" in r and "title" in r and "page" in r
                        and r["title"] in original_titles]
            if not verified:
                return candidates, "all_rejected"
            return verified, "ok"
    except Exception as e:
        print(f"[warn] Ollama 보정 실패: {e} → 휴리스틱 결과 사용")
        return candidates, f"error: {e}"


async def embed_and_write_index(company, period, doc_type, notes, detected_unit,
                                source_type, src_pdf, progress_cb=None) -> dict:
    """notes(확정) → 제목·청크 임베딩 + index.json(schema=2) 기록 + 카탈로그 upsert.
    progress_cb(frac:float) 있으면 노트별 진행률 보고(없으면 무시). INDEX_STATUS 미접근.
    반환: {"notes_count","n_conn","n_sep","detected_unit","total_pages"}."""
    dt = validate_doc_type(doc_type)

    # 제목 + 본문 청크 임베딩(Phase A). doc 1회 오픈으로 청크 본문 추출.
    with fitz.open(src_pdf) as doc:
        total_pages = doc.page_count
        for i, note in enumerate(notes):
            # 본문 청크: 각 청크 임베딩+토큰(페이지 보존 → 인용 정밀)
            chunks = _note_chunks(doc, note.get("page_start"), note.get("page_end"))
            # Phase2: 제목+청크 일괄 배치 임베딩(단건 호출 대비 수십 배 — 재인덱싱 현실화)
            embs = await make_embeddings([note["title"]] + [ch["text"] for ch in chunks])
            note["embedding"] = _round_emb(embs[0])
            note["tokens"] = tokenize_korean(note["title"])
            for ch, ce in zip(chunks, embs[1:]):
                ch["embedding"] = _round_emb(ce)
                ch["tokens"] = tokenize_korean(ch["text"])
            note["chunks"] = chunks
            # Phase2: 본문 전문 보존(검색 스니펫·RAG 컨텍스트용 — 임베딩 없음, 텍스트만)
            note["full_text"] = _note_full_text(doc, note.get("page_start"),
                                                note.get("page_end"))
            if progress_cb:
                progress_cb((i + 1) / max(len(notes), 1))

    # 연결/별도 분리 카운트 (full_report 에서 의미. slim 은 전부 연결)
    n_conn = sum(1 for n in notes if n.get("fs_div") == "연결")
    n_sep = sum(1 for n in notes if n.get("fs_div") == "별도")

    idx_path = index_path(company, period, dt)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump({
            "company": company,
            "period": period,
            "doc_type": dt,
            "schema": INDEX_SCHEMA,
            "total_pages": total_pages,
            "detected_unit": detected_unit,
            "source_type": source_type,
            "notes": notes,
        }, f, ensure_ascii=False)

    # 카탈로그 1행/(회사,기간) 유지 — 기존 행을 읽어 머지 후 upsert(통째 교체 방지).
    # company·period 는 위치 인자로 전달하므로 splat 대상에서 제외 (중복 인자 방지).
    existing = next((e for e in load_catalog()["entries"]
                     if e["company"] == company and e["period"] == period), {})
    existing = {k: v for k, v in existing.items() if k not in ("company", "period")}

    if dt == "review_sep":
        # review_sep 인덱싱 → review_sep_* 접두 필드만 갱신, review 필드 보존.
        # 별도검토보고서는 연결 0/별도 N 이 정상(n_conn≈0). 별도 예외처리 없이 기존 카운트 경로 사용.
        updates = {
            "review_sep_indexed": True,
            "review_sep_notes_count": len(notes),
            "review_sep_notes_count_별도": n_sep,
            "review_sep_notes_count_연결": n_conn,
            "review_sep_source_type": source_type,
            "review_sep_detected_unit": detected_unit,
            "review_sep_indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    elif dt == "report":
        # report 인덱싱 → report_* 접두 필드만 갱신, review/review_sep 보존.
        # 사업보고서는 연결·별도 혼재 → 합계 카운트만 의미. fs_div 별 카운트는 참고용.
        updates = {
            "report_indexed": True,
            "report_notes_count": len(notes),
            "report_notes_count_연결": n_conn,
            "report_notes_count_별도": n_sep,
            "report_source_type": source_type,
            "report_detected_unit": detected_unit,
            "report_indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        # review 인덱싱 → review_* 접두 필드만 갱신, review_sep_* 보존.
        updates = {
            "review_indexed": True,
            "review_notes_count": len(notes),
            "review_notes_count_연결": n_conn,
            "review_notes_count_별도": n_sep,
            "review_source_type": source_type,
            "review_detected_unit": detected_unit,
            "review_indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    upsert_catalog_entry(company, period, **{**existing, **updates})

    return {
        "notes_count": len(notes),
        "n_conn": n_conn,
        "n_sep": n_sep,
        "detected_unit": detected_unit,
        "total_pages": total_pages,
    }


BODY_MAX_CHUNKS_PER_PAGE = 6      # 본문 페이지당 청크 상한
BODY_MAX_CHUNKS_TOTAL = 4000      # 문서당 본문 청크 총상한(인덱스 폭주 방지)


def body_index_path(company: str, period: str, doc_type: str) -> Path:
    """전체 본문(주석 외 포함) 검색 인덱스 경로. 주석 인덱스(index_{dt}.json)와 별도."""
    return entry_dir(company, period) / f"index_body_{validate_doc_type(doc_type)}.json"


async def build_body_index(company, period, doc_type, src_pdf, default_fs_div,
                           progress_cb=None, out_path: Optional[Path] = None) -> dict:
    """PDF 전체 페이지를 본문 유닛(페이지 단위)으로 청킹·임베딩 → index_body_{dt}.json.

    주석 페이지 포함(전체 텍스트 검색). 재무수치(fs_structured.json)·주석 인덱스와는 별개의
    검색 인덱스로, RAG 질의 시 병합돼 감사의견·재무제표 본표 등 비주석 본문도 검색된다.
    유닛 구조는 주석과 동일(no/title/fs_div/embedding/chunks) → retrieve 가 동일 처리.
    """
    dt = validate_doc_type(doc_type)
    units, total_chunks = [], 0
    with fitz.open(src_pdf) as doc:
        total_pages = doc.page_count
        for p in range(1, total_pages + 1):
            if total_chunks >= BODY_MAX_CHUNKS_TOTAL:
                break
            ctexts = [c for c in _chunk_text(doc[p - 1].get_text())
                      if len(c.strip()) >= 20][:BODY_MAX_CHUNKS_PER_PAGE]
            if not ctexts:
                continue
            embs = await make_embeddings(ctexts)
            chunks = [{"text": c, "page": p, "embedding": _round_emb(e),
                       "tokens": tokenize_korean(c)} for c, e in zip(ctexts, embs)]
            total_chunks += len(chunks)
            units.append({
                "no": f"B{p}", "title": f"본문 p.{p}", "fs_div": default_fs_div,
                "page_start": p, "page_end": p,
                "embedding": _round_emb(embs[0]), "tokens": tokenize_korean(ctexts[0]),
                "chunks": chunks, "is_body": True,
            })
            if progress_cb:
                progress_cb(p / max(total_pages, 1))
    with open(out_path or body_index_path(company, period, dt), "w", encoding="utf-8") as f:
        json.dump({"company": company, "period": period, "doc_type": dt,
                   "schema": INDEX_SCHEMA, "total_pages": total_pages,
                   "default_fs_div": default_fs_div, "notes": units}, f, ensure_ascii=False)
    return {"pages_indexed": len(units), "chunks": total_chunks}


async def index_entry(company: str, period: str, doc_type: str = "review"):
    dt = validate_doc_type(doc_type)
    key = f"{company}/{period}/{dt}"
    src_pdf = pdf_path(company, period, dt)
    if not src_pdf.exists():
        INDEX_STATUS[key] = {"status": "error", "error": "PDF not found"}
        return

    INDEX_STATUS[key] = {"status": "running", "progress": 0.0, "stage": "extracting"}

    # review_sep(별도검토보고서)는 slim 노트를 별도로 태깅. review 는 "연결" 기본.
    default_fs_div = "별도" if dt == "review_sep" else "연결"
    notes, detected_unit, source_type = extract_notes_heuristic(src_pdf, default_fs_div=default_fs_div)
    INDEX_STATUS[key]["progress"] = 0.3

    # LLM 보정 단계 (선택적)
    if _OLLAMA_AVAILABLE and notes:
        INDEX_STATUS[key]["stage"] = "llm_refining"
        candidates = [{"no": n["no"], "title": n["title"], "page": n["page_start"]} for n in notes]
        sample_text = ""
        with fitz.open(src_pdf) as doc:
            for pno in range(min(notes[0]["page_start"] - 1 + 5, doc.page_count)):
                sample_text += doc[pno].get_text() + "\n"
                if len(sample_text) > 5000:
                    break
        refined, refine_status = await llm_refine_notes(candidates, sample_text)
        INDEX_STATUS[key]["llm_refine_status"] = refine_status

        # 보정된 후보를 원본 notes에 매핑하여 page_range 보존
        if refine_status == "ok":
            kept_titles = {r["title"] for r in refined}
            notes = [n for n in notes if n["title"] in kept_titles]
    else:
        INDEX_STATUS[key]["llm_refine_status"] = "skipped"

    INDEX_STATUS[key]["stage"] = "embedding"
    INDEX_STATUS[key]["progress"] = 0.5

    res = await embed_and_write_index(
        company, period, dt, notes, detected_unit, source_type, src_pdf,
        progress_cb=lambda f: INDEX_STATUS[key].update(progress=0.5 + 0.5 * f),
    )

    # 전체 본문 인덱스(주석 외 영역 포함) — 재무수치와 별개의 검색 인덱스. 실패해도 주석 검색은 유지.
    body_pages = 0
    try:
        INDEX_STATUS[key]["stage"] = "body_indexing"
        body = await build_body_index(company, period, dt, src_pdf, default_fs_div)
        body_pages = body.get("pages_indexed", 0)
    except Exception as e:
        print(f"[index_entry] 본문 인덱스 생성 실패(무시) {key} - {_safe_err(e)}", file=sys.stderr)

    INDEX_STATUS[key] = {
        "status": "done",
        "progress": 1.0,
        "notes_extracted": res["notes_count"],
        "body_pages": body_pages,
        "detected_unit": res["detected_unit"],
    }


@app.post("/api/library/index/{company}/{period}")
async def start_index(company: str, period: str, background: BackgroundTasks,
                      doc_type: str = "review"):
    # doc_type 은 쿼리 파라미터 — 경로 세그먼트 추가 금지(기존 라우트 충돌 방지).
    validate_company(company)
    validate_period(period)
    dt = validate_doc_type(doc_type)
    if not pdf_path(company, period, dt).exists():
        raise HTTPException(404, "PDF가 업로드되지 않았습니다.")
    # 동시 중복 인덱싱 가드 — 이미 진행 중이면 재시작 차단(상태 덮어쓰기/경합 방지)
    key = f"{company}/{period}/{dt}"
    if INDEX_STATUS.get(key, {}).get("status") == "running":
        raise HTTPException(409, "이미 인덱싱이 진행 중입니다.")
    background.add_task(index_entry, company, period, dt)
    return {"status": "running", "company": company, "period": period, "doc_type": dt}


# ----------------------------------------------------------------------------
# 2b) DART 자동 수집 — collect_dart 를 감싸 UI 버튼에서 호출
# ----------------------------------------------------------------------------
def _period_to_year_reprt(period: str):
    """라이브러리 period('2025Q3')→(year, reprt_code). Q4 등 DART 미지원 시 400."""
    year = int(period[:4])
    reprt = _SUFFIX_TO_REPRT.get(period[4:])
    if not reprt:
        raise HTTPException(400, f"DART 수집 미지원 기간: {period} (Q1/Q2/Q3/FY 만 지원)")
    return year, reprt


def _safe_err(e: Exception) -> str:
    """예외 메시지를 사용자 노출용으로 정제 — 마스킹은 safety 모듈로 중앙화."""
    return safety.safe_err(e)


def _collect_company_blocking(company: str, year: int, reprt: str,
                              period: str, include: dict) -> dict:
    """blocking DART 수집(httpx 동기) — asyncio.to_thread 로 실행.

    corp_code 해결(라이브 corpCode→실패 시 캐시/시드) 후 collect_company 호출.
    키는 .env 에서만 읽고 절대 반환/로그에 노출하지 않는다.
    """
    api_key = cdart.get_api_key()
    if not api_key:
        raise RuntimeError("DART_API_KEY 미설정(.env)")
    with httpx.Client(timeout=60.0) as client:
        try:
            corp_codes = cdart.fetch_and_store_corp_codes(client, api_key)
        except Exception:
            corp_codes = cdart.resolve_corp_codes(cdart._load_cached_corp_codes())
        corp_code = corp_codes.get(company) or cdart.SEED_CORP_CODES.get(company)
        if not corp_code:
            raise RuntimeError(f"{company} corp_code 미해결")
        odr = None
        if cdart._HAS_OPENDART:
            try:
                odr = cdart.OpenDartReader(api_key)
            except Exception:
                odr = None  # review 첨부만 스킵, 나머지 수집은 진행
        return cdart.collect_company(client, api_key, company, corp_code, year, reprt,
                                     period, odr=odr,
                                     collect_review=include.get("review", True),
                                     collect_review_sep=include.get("review_sep", True),
                                     collect_report=include.get("report", False))


def _doc_detail(company: str, period: str, d: dict) -> dict:
    """meta.documents[] 1건 → 수집 상태 표시용 상세 1건.

    수집 여부는 *_collected 플래그가 아닌 디스크 실파일 기준(무클로버 재수집 시
    플래그=False 여도 기존 PDF 가 존재·인덱싱되므로). 인덱싱 수치는 직전
    index_entry 가 남긴 INDEX_STATUS 에서 읽어 카탈로그 재독을 피한다.

    existing: 디스크엔 있으나 이번 런이 받은 게 아닌 파일(무클로버 스킵/합성 항목).
    이번 런 수집 여부는 meta 항목에 fetch 결과 필드(file/filename_dart)가 있는지로 판정
    — UI 가 "수집 ✓(신규)" 대신 "보유"로 구분 표시(이번 런 결과로 오인 방지).
    """
    dt = d.get("doc_type")
    ist = INDEX_STATUS.get(f"{company}/{period}/{dt}", {})
    collected = pdf_path(company, period, dt).exists() if dt else False
    fetched_this_run = bool(d.get("file") or d.get("filename_dart"))
    # 무클로버 재수집은 meta 에 file 이 비어도 디스크엔 표준 작업본이 있음 → 표준명 폴백.
    saved = d.get("file") or (pdf_path(company, period, dt).name if collected else None)
    orig = d.get("filename_original") or original_pdf_name(company, period, dt)
    return {
        "doc_type": dt,
        "filename_dart": d.get("filename_dart"),
        "filename_original": orig,
        "file": saved,
        "renamed": bool(saved and orig and saved != orig),
        "collected": collected,
        "existing": collected and not fetched_this_run,
        "indexed": ist.get("status") == "done",
        "notes_count": ist.get("notes_extracted"),
        "detected_unit": ist.get("detected_unit"),
    }


def _collect_details(company: str, period: str, meta: dict) -> dict:
    """수집 done/error 상태에 첨부할 상세 — 문서별 파일명·인덱싱, 재무 fs_div, 저장경로.

    meta.documents[] 는 이번 런에서 새로 받은 문서만 담는다(무클로버 스킵분 누락).
    디스크에 작업본이 있는 문서유형은 합성 항목으로 보강해 인덱싱 현황까지 빠짐없이 표시.
    """
    docs = {d.get("doc_type"): _doc_detail(company, period, d)
            for d in (meta.get("documents") or []) if d.get("doc_type")}
    for dt in VALID_DOC_TYPES:
        if dt not in docs and pdf_path(company, period, dt).exists():
            docs[dt] = _doc_detail(company, period, {"doc_type": dt})
    order = {"review_sep": 0, "review": 1}
    return {
        "documents": sorted(docs.values(), key=lambda x: order.get(x["doc_type"], 9)),
        "fs_divs": meta.get("fs_divs") or [],
        "fetch_failures": meta.get("fetch_failures") or {},
        # 공시 뷰어 링크는 데이터로 전달 — index.html 에 외부 호스트 하드코딩 금지(폐쇄망 게이트).
        "viewer_url": (cdart.dart_viewer_url(meta["rcept_no"])
                       if meta.get("rcept_no") else None),
        "path": str(entry_dir(company, period)),
    }


async def _collect_and_index(company: str, period: str, include: dict):
    """백그라운드: DART 수집(스레드) → 카탈로그 등록 → 확보된 표시용 PDF 자동 인덱싱.

    - include: 사용자가 선택한 수집 문서 {"review","review_sep" → bool}.
      재무데이터(fnlttSinglAcntAll)는 선택과 무관하게 항상 수집된다.
    - 카탈로그 등록은 인덱싱 성공과 분리한다. 검토보고서 PDF 를 못 받아도(재무데이터만
      수집) 라이브러리에 등록돼야 한다(과거: 인덱싱 0건이면 미등록 → 셀이 빈칸으로 남음).
    - 인덱싱은 불안정한 *_collected 플래그가 아니라 '디스크에 실제 존재하는 PDF' 기준,
      단 사용자가 선택한 문서유형만(미선택 문서의 불필요한 재인덱싱 방지).
    """
    key = f"{company}/{period}"
    COLLECT_STATUS[key] = {"status": "running", "stage": "collecting"}
    try:
        year, reprt = _period_to_year_reprt(period)
        meta = await asyncio.to_thread(_collect_company_blocking, company, year, reprt,
                                       period, include)
    except Exception as e:
        COLLECT_STATUS[key] = {"status": "error", "error": _safe_err(e),
                               "requested": include}
        return

    review_ok = bool(meta.get("review_collected"))
    review_sep_ok = bool(meta.get("review_sep_collected"))
    report_ok = bool(meta.get("report_collected"))
    fs_ok = (entry_dir(company, period) / "fs_structured.json").exists()

    # (1) 수집 시점에 카탈로그 등록 — 인덱싱과 독립. 기존 행 필드는 보존(머지).
    existing = next((e for e in load_catalog()["entries"]
                     if e["company"] == company and e["period"] == period), {})
    existing = {k: v for k, v in existing.items() if k not in ("company", "period")}
    upsert_catalog_entry(company, period, **{
        **existing,
        "rcept_no": meta.get("rcept_no"),
        "report_nm": meta.get("report_nm"),
        "review_collected": review_ok,
        "review_sep_collected": review_sep_ok,
        "report_collected": report_ok,
        "fs_collected": fs_ok,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    })

    COLLECT_STATUS[key] = {"status": "indexing", "stage": "indexing",
                           "review_collected": review_ok,
                           "review_sep_collected": review_sep_ok,
                           "report_collected": report_ok}

    # (2) 디스크에 존재하는 표시용 PDF 인덱싱 — 사용자가 선택한 문서유형만.
    indexed: List[str] = []
    try:
        for dt in ("review", "review_sep", "report"):
            if include.get(dt, False) and pdf_path(company, period, dt).exists():
                await index_entry(company, period, dt)
                indexed.append(dt)
    except Exception as e:
        # 인덱싱 실패여도 수집·카탈로그 등록은 끝난 상태 — 부분 성공 문서 상세를 함께 노출.
        COLLECT_STATUS[key] = {"status": "error", "error": _safe_err(e),
                               "review_collected": review_ok,
                               "review_sep_collected": review_sep_ok,
                               "report_collected": report_ok,
                               "requested": include,
                               **_collect_details(company, period, meta)}
        return

    COLLECT_STATUS[key] = {
        "status": "done",
        "review_collected": review_ok,
        "review_sep_collected": review_sep_ok,
        "report_collected": report_ok,
        "fs_collected": fs_ok,
        "indexed": indexed,
        "rcept_no": meta.get("rcept_no"),
        "report_nm": meta.get("report_nm"),
        "requested": include,
        **_collect_details(company, period, meta),
    }


class CollectPayload(BaseModel):
    company: str
    period: str
    include_review: bool = True           # 연결재무제표 검토보고서
    include_review_sep: bool = True       # 별도재무제표 검토보고서
    include_report: bool = False          # 사업보고서 본문(best-effort — DART 첨부 미제공 시 업로드 폴백)


@app.post("/api/library/collect")
async def start_collect(payload: CollectPayload, background: BackgroundTasks):
    company = validate_company(payload.company)
    period = validate_period(payload.period)
    _period_to_year_reprt(period)  # Q4 등 미지원 기간 조기 차단
    if not cdart.get_api_key():
        raise HTTPException(400, "DART_API_KEY 가 설정되지 않았습니다. backend/.env 에 키를 추가하세요.")
    key = f"{company}/{period}"
    if COLLECT_STATUS.get(key, {}).get("status") in ("running", "indexing"):
        raise HTTPException(409, "이미 수집이 진행 중입니다.")
    COLLECT_STATUS[key] = {"status": "running", "stage": "queued"}
    include = {"review": payload.include_review,
               "review_sep": payload.include_review_sep,
               "report": payload.include_report}
    background.add_task(_collect_and_index, company, period, include)
    return {"status": "running", "company": company, "period": period}


# ----------------------------------------------------------------------------
# 3) Compare — 비교 대상 명시 후 검색·매칭 (하이브리드)
# ----------------------------------------------------------------------------
class CompareTarget(BaseModel):
    company: str
    period: str
    # 미지정 시 "review"(연결재무제표 검토보고서). report=사업보고서(연결·별도 중립).
    doc_type: Optional[Literal["review", "review_sep", "report"]] = "review"
    # 연결/별도 1급 차원. 동일 fs_div 끼리만 비교(연결↔연결, 별도↔별도). "all"=전체.
    fs_div: Optional[Literal["연결", "별도", "all"]] = "연결"


class ComparePayload(BaseModel):
    targets: List[CompareTarget] = Field(..., min_length=1, max_length=12)
    query: str
    mode: Literal["topic", "number"] = "topic"
    note_kind: Optional[Literal["전체", "주기", "서술형"]] = "전체"  # 주석 종류 필터
    rerank: bool = False  # Phase4: Ollama 후보 재정렬 옵트인(가용 시에만, 실패 무시)


def cosine(a, b):
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


SEARCH_RERANK_TIMEOUT = float(os.getenv("SEARCH_RERANK_TIMEOUT", "6.0"))


async def llm_rerank_candidates(query: str, candidates: list) -> list:
    """검색개선 Phase4 — Ollama 로 후보 '순서만' 재조정(점수·메타 불변, 옵트인).

    임베딩이 놓치는 의미 관계(용어 변형 등)를 LLM 판단으로 보정한다.
    할루시네이션 차단: LLM 은 기존 후보의 note_no 배열만 반환 — 새 항목 추가 불가.
    실패/타임아웃/비가용 시 원본 순서 그대로(무회귀).
    """
    if not _OLLAMA_AVAILABLE or len(candidates) < 3:
        return candidates
    items = [{"no": c["note_no"], "title": c["title"]} for c in candidates]
    prompt = f"""한국 금융지주 재무제표 주석 검색 후보를 질의 관련성 순으로 재정렬하는 도구입니다.

질의: {query}
후보: {json.dumps(items, ensure_ascii=False)}

규칙: 위 후보의 no 값만 사용하세요. 관련성이 높은 순서의 no 배열(JSON)만 반환. 설명 금지.
형식: [3, 17, 1]"""
    try:
        async with httpx.AsyncClient(timeout=SEARCH_RERANK_TIMEOUT) as client:
            res = await client.post(f"{OLLAMA_URL}/api/generate", json={
                "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": 200, "top_p": 1.0}})
            res.raise_for_status()
            text = res.json().get("response", "").strip()
        m = re.search(r"\[[\d,\s]*\]", text)
        if not m:
            return candidates
        order = [n for n in json.loads(m.group()) if isinstance(n, int)]
        if not order:
            return candidates
        # note_no 순서로 재배열 — 미언급 후보는 기존 순서대로 뒤에 보존(누락 금지).
        remaining = list(candidates)
        out = []
        for no in order:
            hit = next((c for c in remaining if c["note_no"] == no), None)
            if hit is not None:
                out.append(hit)
                remaining.remove(hit)
        out.extend(remaining)
        for c in out:
            c["reranked"] = True  # UI 표시용 — AI 개입 투명화
        return out
    except Exception as e:
        print(f"[llm_rerank] 실패(무시 — 기존 순서 유지): {type(e).__name__}", file=sys.stderr)
        return candidates


def _notes_for_comparison(idx: dict, fs_div: str = "연결") -> list:
    """비교 대상 note 목록 — 동일 fs_div 끼리만 비교하도록 필터(연결/별도 1급 차원).

    - fs_div="all": 전체 note 반환(연결+별도).
    - fs_div="연결"|"별도": note 에 fs_div 태그가 있으면 해당 그룹만 반환.
      태그가 전혀 없는 구(舊) 인덱스는 전량 통과(하위호환). 슬림(검토보고서) 노트는
      전부 "연결" 태깅돼 있어 "별도" 요청 시 빈 목록 → 호출부에서 미지원으로 처리.
    """
    notes = idx.get("notes", [])
    if fs_div == "all":
        return notes
    has_fs_div = any(n.get("fs_div") for n in notes)
    if not has_fs_div:
        return notes
    return [n for n in notes if n.get("fs_div") == fs_div]


def _note_unit_cos(q_emb, note: dict):
    """노트 유닛(제목+본문청크) cosine — max(0.6) + 상위3 평균(0.4) 혼합 점수와 최고 페이지.

    Phase1: max-only 는 여러 청크가 고르게 매칭돼도 가산이 없어 다중 근거 노트가
    단발 스파이크 노트와 동점이 됐다 → 상위3 평균을 섞어 매칭 폭을 반영.
    유닛 1개(구 인덱스 제목만/청크 없음)면 평균=최댓값이라 기존 점수와 동일(무회귀).
    """
    units = []
    if note.get("embedding"):
        units.append((cosine(q_emb, note["embedding"]), note.get("page_start")))
    for ch in note.get("chunks", []):
        e = ch.get("embedding")
        if e:
            units.append((cosine(q_emb, e), ch.get("page", note.get("page_start"))))
    if not units:
        return -1.0, note.get("page_start")
    units.sort(key=lambda u: -u[0])
    best, page = units[0]
    top3 = [s for s, _ in units[:3]]
    return UNIT_MAX_W * best + (1.0 - UNIT_MAX_W) * (sum(top3) / len(top3)), page


def _score_notes_for_query(q_emb, q_text: str, notes_with_emb: list) -> list:
    """전 노트의 final score·메타를 계산하는 공유 스코어링 코어(순수 규칙, AI 미사용).

    match_query_in_notes(argmax)·rank_notes_for_query(top-k) 가 동일 점수/임계/라벨을
    공유하도록 추출. 정렬·상위선택 정책은 호출부가 결정(여기선 노트 순서 보존).
    - Phase C: cosine·BM25 를 제목+본문청크로 확장. cosine=유닛 최댓값,
      BM25 코퍼스=제목+청크 토큰 합본. match_page=최고 유닛의 실제 페이지(인용 정밀).
    - lexical_hit: 질의 토큰이 제목 또는 청크 본문에 글자 그대로 존재하는가.
    - keep: 점수 플로어 통과 또는 (어휘 일치 + 완화 하한 통과).
    - confidence: 고점 + 어휘 일치 동시 충족 시에만 high, 그 외 low.

    Returns:
        notes_with_emb 와 동일 순서의 후보 dict 리스트(빈 입력이면 빈 리스트).
        각 dict: note_no/title/page_start/page_end/match_page/score(float)/
                 confidence/lexical_hit/keep/note_kind/min_match.
    """
    if not notes_with_emb:
        return []

    scored = [_note_unit_cos(q_emb, n) for n in notes_with_emb]
    cos_scores = np.array([s for s, _ in scored])
    match_pages = [p for _, p in scored]

    # Phase E: BM25·lexical 질의에 동의어 확장(임베딩 q_emb 는 원질의 유지 → 정밀도 보존).
    q_text_exp = synonyms.expand_query(q_text)

    if USE_BM25 and _HAS_BM25:
        # Phase1: 제목/청크 코퍼스 분리 — 합본 코퍼스에선 제목 토큰(~5개)이 청크
        # 토큰(수백 개)에 희석돼 제목 고신호가 묻혔다. 각각 정규화 후 가중 결합.
        title_corpus, chunk_corpus = [], []
        for n in notes_with_emb:
            t = list(n.get("tokens") or tokenize_korean(n["title"]))
            c: list = []
            for ch in n.get("chunks", []):
                c.extend(ch.get("tokens") or [])
            title_corpus.append(t or ["_"])
            chunk_corpus.append(c or t or ["_"])
        q_toks = tokenize_korean(q_text_exp)

        def _bm_norm(arr):
            # BM25Okapi 는 소코퍼스에서 음수 점수를 낼 수 있다 — 음수를 0으로 클립 후
            # max>0 일 때만 정규화(기존 max(…,1e-9) 나눗셈은 음수를 수백만 배 증폭).
            arr = np.clip(arr, 0.0, None)
            m = arr.max()
            return arr / m if m > 0 else arr

        bm_t = _bm_norm(BM25Okapi(title_corpus).get_scores(q_toks))
        bm_c = _bm_norm(BM25Okapi(chunk_corpus).get_scores(q_toks))
        bm_norm = BM25_TITLE_W * bm_t + (1.0 - BM25_TITLE_W) * bm_c
        # B-1: note_kind 별 하이브리드 가중 — 주기(정책 서술)는 임베딩 비중↑, 서술형은 현행.
        kinds = [note_filters.note_kind(n.get("title", "")) for n in notes_with_emb]
        w_cos = np.array([COS_W_POLICY if k == "주기" else COS_W_DEFAULT for k in kinds])
        w_bm = np.array([BM_W_POLICY if k == "주기" else BM_W_DEFAULT for k in kinds])
        final = w_cos * cos_scores + w_bm * bm_norm
    else:
        final = cos_scores

    q_tokens = tokenize_korean(q_text_exp)
    out = []
    for i, n in enumerate(notes_with_emb):
        score = float(final[i])
        lexical_hit = any(qt in n["title"] for qt in q_tokens) or any(
            qt in (ch.get("text") or "") for ch in n.get("chunks", []) for qt in q_tokens)
        # B-2: 주기(정책 서술)는 키워드 빈약 → 채택 하한 완화. 서술형은 현행 MIN_MATCH_SCORE.
        note_kind = note_filters.note_kind(n.get("title", ""))
        min_match = POLICY_MIN_MATCH if note_kind == "주기" else MIN_MATCH_SCORE
        keep = (score >= min_match) or (lexical_hit and score >= LEXICAL_FLOOR)
        confidence = "high" if (score >= HIGH_CONF and lexical_hit) else "low"
        out.append({
            "note_no": n["no"],
            "title": n["title"],
            "fs_div": n.get("fs_div"),  # 후보 자체의 연결/별도(타깃 all 검색 시 구분용)
            "page_start": n["page_start"],
            "page_end": n["page_end"],
            "match_page": match_pages[i],
            "score": score,
            "confidence": confidence,
            "lexical_hit": lexical_hit,
            "keep": keep,
            "note_kind": note_kind,   # 주기/서술형 — 가중·임계 분기 근거(투명성)
            "min_match": round(min_match, 3),
        })
    return out


def match_query_in_notes(q_emb, q_text: str, notes_with_emb: list) -> Optional[dict]:
    """질의(임베딩+원문)를 한 회사 주석목록에 매칭 — final score argmax 단일 노트.

    compare()/coverage·structure-diff·topic-map 이 의존(무변경). 점수/임계/라벨은
    _score_notes_for_query 공유. 동률은 argmax 관례대로 최저 인덱스(결정론).

    Args:
        q_emb: 질의 임베딩 (np.ndarray)
        q_text: 질의 원문 (BM25·lexical 용)
        notes_with_emb: "embedding" 키를 가진 note dict 리스트(chunks 선택적)
    Returns:
        최고점 후보 dict 또는 후보 없음(빈 목록) 시 None.
    """
    cands = _score_notes_for_query(q_emb, q_text, notes_with_emb)
    if not cands:
        return None
    # 기존 np.argmax 동률 정책(최저 인덱스) 보존 — 무회귀.
    best_idx = int(np.argmax([c["score"] for c in cands]))
    return cands[best_idx]


def rank_notes_for_query(q_emb, q_text: str, notes_with_emb: list, k: int = 5) -> list:
    """질의에 대한 상위 k 후보를 score 내림차순으로 반환(낮은 신뢰도·keep=False 포함).

    주석 비교조회(/api/compare topic) 가 회사별 후보 목록을 보여주기 위한 진입점.
    match_query_in_notes 와 동일 스코어링(_score_notes_for_query) 을 재사용해 일관 보장.
    정렬: score desc, 동점이면 note_no asc(결정론 — 모델·플랫폼 불문 동일 순서).

    Args:
        q_emb: 질의 임베딩
        q_text: 질의 원문
        notes_with_emb: "embedding" 키 보유 note dict 리스트
        k: 반환 상한(기본 5)
    Returns:
        후보 dict 리스트(≤k). score 는 round3. keep=False 후보도 숨기지 않음.
    """
    cands = _score_notes_for_query(q_emb, q_text, notes_with_emb)
    cands.sort(key=lambda c: (-c["score"], c["note_no"]))
    top = cands[:max(0, k)]
    for c in top:
        c["score"] = round(c["score"], 3)
    return top


@app.post("/api/compare")
async def compare(payload: ComparePayload):
    matches, missing = [], []
    units_seen = set()

    q_emb = await make_embedding(payload.query) if payload.mode == "topic" else None

    for target in payload.targets:
        # doc_type 별 인덱스 경로 선택(기본 review). 비교 로직 자체는 인덱스-불가지(무변경).
        idx_path = index_path(target.company, target.period, target.doc_type)
        if not idx_path.exists():
            missing.append({"company": target.company, "period": target.period,
                            "doc_type": target.doc_type, "reason": "not indexed"})
            continue

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        detected_unit = idx.get("detected_unit")

        if payload.mode == "number":
            try:
                target_no = int(payload.query)
            except ValueError:
                raise HTTPException(400, "번호 모드에는 숫자만 입력 가능합니다.")
            hit = next((n for n in note_filters.filter_notes(_notes_for_comparison(idx, target.fs_div), payload.note_kind) if n["no"] == target_no), None)
            if hit is None:
                # 정확매칭 실패 = 진짜 미발견. UI 통일을 위해 candidates 빈 목록 대신 missing.
                missing.append({"company": target.company, "period": target.period,
                                "doc_type": target.doc_type, "reason": "no match"})
                continue
            # number 모드: 결정론 정확매칭을 단일 후보로 통일(AI 무경유·페이지 인용 유지).
            candidates = [{
                "note_no": hit["no"],
                "title": hit["title"],
                "page_start": hit["page_start"],
                "page_end": hit["page_end"],
                "match_page": hit["page_start"],
                "score": 1.0,
                "confidence": "exact",
                "lexical_hit": None,
                "keep": True,
            }]
        else:
            notes_with_emb = [n for n in note_filters.filter_notes(_notes_for_comparison(idx, target.fs_div), payload.note_kind) if "embedding" in n]
            if not notes_with_emb:
                # missing 은 오직 인덱스 없음/임베딩 없음. 임계 미달은 후보로 노출(숨기지 않음).
                missing.append({"company": target.company, "period": target.period,
                                "doc_type": target.doc_type, "reason": "no embeddings"})
                continue
            # top-k 후보(낮은 신뢰도 포함). below_threshold 라도 missing 으로 보내지 않음.
            candidates = rank_notes_for_query(q_emb, payload.query, notes_with_emb,
                                              k=SEARCH_TOP_K)
            # Phase4: LLM 재정렬(옵트인) — 순서만 조정, 실패 시 무시.
            if payload.rerank:
                candidates = await llm_rerank_candidates(payload.query, candidates)

        matches.append({
            "company": target.company,
            "period": target.period,
            "doc_type": target.doc_type,
            "fs_div": target.fs_div,
            "detected_unit": detected_unit,
            "candidates": candidates,
        })
        if detected_unit:
            units_seen.add(detected_unit)

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "query": payload.query,
        "mode": payload.mode,
        "matches": matches,
        "missing": missing,
        "unit_warning": len(units_seen) > 1,
        "units_seen": sorted(units_seen),
        "embedding_backend": embedding_backend,
        "thresholds": {
            "min_match_score": MIN_MATCH_SCORE,
            "lexical_floor": LEXICAL_FLOOR,
            "high_conf": HIGH_CONF,
        },
    }


# ----------------------------------------------------------------------------
# 4) Insights — 신한 관점 인사이트 (A 커버리지 매트릭스 / B 구조 차집합)
#    순수 규칙·임베딩만 사용. 숫자 재구성·단위 환산 없음, 외부 호출 없음.
# ----------------------------------------------------------------------------
class CoveragePayload(BaseModel):
    targets: List[CompareTarget] = Field(..., min_length=1, max_length=12)
    topics: Optional[List[str]] = None  # 생략 시 DEFAULT_TOPICS
    note_kind: Optional[Literal["전체", "주기", "서술형"]] = "전체"


def _load_index(company: str, period: str, doc_type: str = "review") -> Optional[dict]:
    """저장된 인덱스 로드(doc_type 별 경로). 없으면 None — 부재 셀 안전 처리."""
    idx_path = index_path(company, period, doc_type)
    if not idx_path.exists():
        return None
    return json.loads(idx_path.read_text(encoding="utf-8"))


def _load_body_index(company: str, period: str, doc_type: str = "review") -> Optional[dict]:
    """전체 본문 인덱스 로드(index_body_{dt}.json). 미생성 셀(재인덱싱 전)은 None."""
    bp = body_index_path(company, period, doc_type)
    if not bp.exists():
        return None
    try:
        return json.loads(bp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _doc_indexed(entry: dict, doc_type: str = "review") -> bool:
    """카탈로그 엔트리의 문서유형별 인덱싱 플래그(review_indexed / review_sep_indexed / report_indexed)."""
    if doc_type == "review_sep":
        return bool(entry.get("review_sep_indexed"))
    if doc_type == "report":
        return bool(entry.get("report_indexed"))
    return bool(entry.get("review_indexed"))


def _load_topic_dict_topics() -> Optional[List[str]]:
    """주제사전 자동초안(topic_dict.json)에서 토픽 라벨 리스트 로드. 없으면 None.

    Step 7: coverage 토픽 미지정 시 우선 소스. 파일이 없거나 비정상이면 None 을
    돌려 호출부가 DEFAULT_TOPICS 로 폴백하게 한다(기존 동작 보존).
    """
    if not TOPIC_DICT_PATH.exists():
        return None
    try:
        data = json.loads(TOPIC_DICT_PATH.read_text(encoding="utf-8"))
        raw = [t["topic"] for t in data.get("topics", []) if t.get("topic")]
        # 중복 라벨 방어 — coverage 매트릭스가 topic 문자열을 키로 쓰므로 유일화(순서보존).
        topics = list(dict.fromkeys(raw))
        return topics or None
    except Exception as e:
        print(f"[warn] topic_dict.json 로드 실패 → DEFAULT_TOPICS 폴백: {e}")
        return None


@app.post("/api/coverage")
async def coverage(payload: CoveragePayload):
    """A. 횡단 커버리지 매트릭스 — 주제 × 회사 → 전용/관련/미발견.

    각 주제를 1회 임베딩 후 각 회사 주석목록에 match_query_in_notes 적용.
    셀 분류: keep=False→none / confidence=="high"→dedicated / 그 외(keep·low)→related.
    """
    # 토픽 소스 결정: 명시 topics > topic_dict.json 자동초안 > DEFAULT_TOPICS 폴백.
    if payload.topics:
        topics = payload.topics
        topic_source = "explicit"
    else:
        auto_topics = _load_topic_dict_topics()
        if auto_topics:
            topics = auto_topics
            topic_source = "topic_dict"
        else:
            topics = DEFAULT_TOPICS
            topic_source = "default"
    units_seen = set()

    # 각 target 인덱스 1회 로드 (재임베딩 없음 — 주제 임베딩만 생성)
    loaded: Dict[str, dict] = {}
    key_fsdiv: Dict[str, str] = {}  # key→fs_div (연결/별도 1급 차원)
    target_keys: List[str] = []
    # 매트릭스 행 키 — 문서유형으로 구분(동일 셀 review/review_sep 동시 비교 시 충돌 방지).
    for t in payload.targets:
        key = f"{t.company}/{t.period}/{t.doc_type}"
        target_keys.append(key)
        key_fsdiv[key] = t.fs_div
        idx = _load_index(t.company, t.period, t.doc_type)
        loaded[key] = idx
        if idx and idx.get("detected_unit"):
            units_seen.add(idx["detected_unit"])

    matrix: Dict[str, Dict[str, Any]] = {}
    for topic in topics:
        q_emb = await make_embedding(topic)
        row: Dict[str, Any] = {}
        for key in target_keys:
            idx = loaded[key]
            if not idx:
                row[key] = {"coverage_level": "none", "best_score": None,
                            "reason": "not indexed"}
                continue
            notes_with_emb = [n for n in note_filters.filter_notes(_notes_for_comparison(idx, key_fsdiv.get(key, "연결")), payload.note_kind) if "embedding" in n]
            m = match_query_in_notes(q_emb, topic, notes_with_emb)
            if m is None:
                row[key] = {"coverage_level": "none", "best_score": None,
                            "reason": "no embeddings"}
                continue
            if not m["keep"]:
                # 임계값 미달 — 미발견(단, 참고용 best_score 노출)
                row[key] = {"coverage_level": "none",
                            "best_score": round(m["score"], 3)}
            else:
                level = "dedicated" if m["confidence"] == "high" else "related"
                row[key] = {
                    "coverage_level": level,
                    "note_no": m["note_no"],
                    "title": m["title"],
                    "page_start": m["page_start"],
                    "page_end": m["page_end"],
                    "score": round(m["score"], 3),
                    "confidence": m["confidence"],
                    "lexical_hit": m["lexical_hit"],
                }
        matrix[topic] = row

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "topics": topics,
        "topic_source": topic_source,
        "targets": [{"company": t.company, "period": t.period} for t in payload.targets],
        "matrix": matrix,
        "unit_warning": len(units_seen) > 1,
        "units_seen": sorted(units_seen),
        "embedding_backend": embedding_backend,
    }


class StructureDiffPayload(BaseModel):
    base: CompareTarget = Field(default_factory=lambda: CompareTarget(company="신한", period="2026Q1"))
    peers: List[CompareTarget] = Field(..., min_length=1, max_length=12)


def _best_cosine(emb, notes_with_emb: list) -> tuple:
    """emb 에 대해 notes_with_emb 중 최고 코사인 note 와 점수 반환. (note, score) 또는 (None, 0.0)."""
    best_note, best_s = None, -1.0
    for n in notes_with_emb:
        s = cosine(emb, n["embedding"])
        if s > best_s:
            best_note, best_s = n, s
    return (best_note, best_s) if best_note is not None else (None, 0.0)


@app.post("/api/structure-diff")
async def structure_diff(payload: StructureDiffPayload):
    """B. 신한 vs 동종 주석 차집합 — 저장된 임베딩으로 의미 매칭(재임베딩 없음).

    note A↔B 대응 = cosine(embA, embB) >= GAP_SIM_THRESHOLD.
    - base_only: base 에 있으나 어느 peer 에도 대응 없는 항목.
    - peer_disclosed_base_missing: ≥1 peer 가 가졌으나 base 에 대응 없는 항목
      (disclosed_by, peer_count 포함, peer_count 내림차순).
    """
    base_idx = _load_index(payload.base.company, payload.base.period, payload.base.doc_type)
    if not base_idx:
        raise HTTPException(404, f"base 인덱스 없음: {payload.base.company}/{payload.base.period}")

    base_notes = [n for n in _notes_for_comparison(base_idx, payload.base.fs_div) if "embedding" in n]

    peer_data: List[Dict[str, Any]] = []  # {company, period, notes}
    for p in payload.peers:
        idx = _load_index(p.company, p.period, p.doc_type)
        if not idx:
            continue
        peer_data.append({
            "company": p.company, "period": p.period,
            "notes": [n for n in _notes_for_comparison(idx, p.fs_div) if "embedding" in n],
        })
    if not peer_data:
        raise HTTPException(404, "대응할 peer 인덱스가 하나도 없습니다.")

    thr = GAP_SIM_THRESHOLD

    # 1) base_only: base note 가 어느 peer 에도 대응 없는 항목
    base_only = []
    for bn in base_notes:
        matched_any = False
        for pd in peer_data:
            _, s = _best_cosine(bn["embedding"], pd["notes"])
            if s >= thr:
                matched_any = True
                break
        if not matched_any:
            base_only.append({
                "note_no": bn["no"], "title": bn["title"],
                "page_start": bn["page_start"], "page_end": bn["page_end"],
            })

    # 2) peer_disclosed_base_missing: peer note 가 base 에 대응 없음
    #    동일 항목(여러 peer 가 같은 주제)을 묶기 위해 peer note 의 base 최고 유사 note 로 그룹화하지 않고,
    #    각 peer note 를 그 "대표 제목" 기준으로 합산한다. 단순 PoC: 제목 정규화 키로 묶음.
    gap_map: Dict[str, Dict[str, Any]] = {}
    for pd in peer_data:
        for pn in pd["notes"]:
            _, s = _best_cosine(pn["embedding"], base_notes)
            if s >= thr:
                continue  # base 에 대응 있음 → 갭 아님
            key = re.sub(r"\s+", "", pn["title"])  # 공백 무시 제목 키
            if key not in gap_map:
                gap_map[key] = {
                    "title": pn["title"],
                    "disclosed_by": [],
                    "base_best_score": round(s, 3),
                    "examples": [],
                }
            entry = gap_map[key]
            company = pd["company"]
            if company not in entry["disclosed_by"]:
                entry["disclosed_by"].append(company)
            entry["examples"].append({
                "company": pd["company"], "period": pd["period"],
                "note_no": pn["no"], "title": pn["title"],
                "page_start": pn["page_start"], "page_end": pn["page_end"],
                "base_best_score": round(s, 3),
            })

    peer_disclosed = []
    for entry in gap_map.values():
        peer_disclosed.append({
            "title": entry["title"],
            "disclosed_by": entry["disclosed_by"],
            "peer_count": len(entry["disclosed_by"]),
            "examples": entry["examples"],
        })
    # peer_count 높은 순(공시 갭 신호 강도), 동률 시 제목순
    peer_disclosed.sort(key=lambda e: (-e["peer_count"], e["title"]))

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "base": {"company": payload.base.company, "period": payload.base.period},
        "peers": [{"company": p["company"], "period": p["period"]} for p in peer_data],
        "threshold": thr,
        "base_only": base_only,
        "peer_disclosed_base_missing": peer_disclosed,
        "embedding_backend": embedding_backend,
    }


# ----------------------------------------------------------------------------
# 5) PDF 스트리밍 — PDF.js가 직접 로드
# ----------------------------------------------------------------------------
@app.get("/api/pdf")
async def serve_pdf(company: str, period: str, doc_type: str = "review"):
    # doc_type 생략 시 review.pdf(연결재무제표 검토보고서).
    target_pdf = pdf_path(company, period, doc_type)
    if not target_pdf.exists():
        raise HTTPException(404, "PDF를 찾을 수 없습니다.")
    # inline: 새 창(팝업) 열람 시 다운로드 대신 브라우저 PDF 뷰어로 표시(#page 이동 지원).
    # 원본명이 있으면 filename*=UTF-8'' 로 부여(한글 파일명 안전). 없으면 기존과 동일(inline 만).
    orig = original_pdf_name(company, period, doc_type)
    if orig:
        disposition = f"inline; filename*=UTF-8''{quote(orig)}"
    else:
        disposition = "inline"
    return FileResponse(target_pdf, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})


# ----------------------------------------------------------------------------
# 회계기준서(Accounting Standards) — 임의 업로드 PDF 독립 문서공간.
# 4개사/기간과 무관. 업로드 → 본문 전체 인덱싱(build_body_index) → 검색(notes_rag.retrieve)
# → 원문 출처·PDF 표시. 결정론·인용강제·검색전용(LLM 생성 없음).
# ----------------------------------------------------------------------------
_STD_ID_RE = re.compile(r"[^\w가-힣]+")


def _slugify_standard(filename: str) -> str:
    """파일명 → 안전한 슬러그(확장자 제거, 비-[\\w가-힣]→_, 길이캡 64)."""
    base = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    slug = _STD_ID_RE.sub("_", base).strip("_")
    return (slug or "standard")[:64]


def _standard_doc_id(filename: str, content: bytes) -> str:
    """doc_id = {slug}_{sha1(content)[:8]}. 동일 PDF 멱등·동명이내용 분리·traversal 차단."""
    return f"{_slugify_standard(filename)}_{hashlib.sha1(content).hexdigest()[:8]}"


def standard_dir(doc_id: str) -> Path:
    return STANDARDS_ROOT / doc_id


def standard_pdf_path(doc_id: str) -> Path:
    return standard_dir(doc_id) / "doc.pdf"


def standard_index_path(doc_id: str) -> Path:
    return standard_dir(doc_id) / "index_body.json"


def load_standards_catalog() -> dict:
    """회계기준서 전용 카탈로그(기존 catalog.json 과 분리). 부재/손상 시 빈 구조."""
    if not STANDARDS_CATALOG_PATH.exists():
        return {"updated_at": None, "docs": []}
    try:
        return json.loads(STANDARDS_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "docs": []}


def save_standards_catalog(cat: dict):
    cat["updated_at"] = datetime.now(timezone.utc).isoformat()
    STANDARDS_CATALOG_PATH.write_text(
        json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_standard(doc_id: str, **fields):
    """doc_id 1행 머지 upsert(기존 필드 보존)."""
    cat = load_standards_catalog()
    existing = next((d for d in cat["docs"] if d.get("doc_id") == doc_id), None)
    if existing:
        existing.update(fields)
    else:
        cat["docs"].append({"doc_id": doc_id, **fields})
    save_standards_catalog(cat)


def remove_standard(doc_id: str):
    cat = load_standards_catalog()
    cat["docs"] = [d for d in cat["docs"] if d.get("doc_id") != doc_id]
    save_standards_catalog(cat)


def _standard_exists(doc_id: str) -> bool:
    """doc_id 화이트리스트(카탈로그 존재) — 경로 traversal 방어용."""
    return any(d.get("doc_id") == doc_id for d in load_standards_catalog()["docs"])


@app.post("/api/standards/upload")
async def standards_upload(file: UploadFile = File(...), title: str = Form("")):
    """회계기준서 PDF 업로드(회사/기간 없음). doc_id 부여 후 카탈로그 등록(미인덱싱)."""
    content = await file.read()
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            pages = doc.page_count
    except Exception:
        raise HTTPException(400, "유효한 PDF가 아닙니다.")
    doc_id = _standard_doc_id(file.filename or "standard.pdf", content)
    d = standard_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    standard_pdf_path(doc_id).write_bytes(content)
    # 원본명 사본 보존(표시·다운로드용). 작업본명(doc.pdf)과 충돌 회피.
    orig = safe_original_filename(file.filename)
    if orig and orig.lower() != "doc.pdf":
        (d / orig).write_bytes(content)
    upsert_standard(
        doc_id,
        title=(title.strip() or (file.filename or doc_id)),
        filename_original=file.filename,
        uploaded_at=datetime.now(timezone.utc).isoformat(),
        pages=pages, size_mb=round(len(content) / (1024 * 1024), 2),
        content_sha1=hashlib.sha1(content).hexdigest(),
        indexed=False, chunks=0, schema=INDEX_SCHEMA,
    )
    return {"doc_id": doc_id, "pages": pages, "indexed": False}


@app.get("/api/standards/list")
async def standards_list():
    """업로드된 회계기준서 목록 + 인덱싱 진행상태."""
    out = []
    for d in load_standards_catalog()["docs"]:
        st = INDEX_STATUS.get(f"standards/{d.get('doc_id')}") or {}
        out.append({**d, "index_status": st.get("status"),
                    "index_progress": st.get("progress")})
    return {"docs": out}


async def _index_standard(doc_id: str):
    """본문 전체 인덱싱(build_body_index 재사용, 출력=standards 전용 경로)."""
    key = f"standards/{doc_id}"
    INDEX_STATUS[key] = {"status": "running", "progress": 0.0}
    src = standard_pdf_path(doc_id)
    if not src.exists():
        INDEX_STATUS[key] = {"status": "error", "error": "PDF 없음"}
        return
    try:
        def _cb(p):
            INDEX_STATUS[key]["progress"] = round(p, 3)
        res = await build_body_index(doc_id, "-", "report", src, "all",
                                     progress_cb=_cb, out_path=standard_index_path(doc_id))
        upsert_standard(doc_id, indexed=True, chunks=res["chunks"],
                        indexed_at=datetime.now(timezone.utc).isoformat())
        INDEX_STATUS[key] = {"status": "done", "chunks": res["chunks"]}
    except Exception as e:
        INDEX_STATUS[key] = {"status": "error", "error": _safe_err(e)}


@app.post("/api/standards/index/{doc_id}")
async def standards_index(doc_id: str, background: BackgroundTasks):
    if not _standard_exists(doc_id):
        raise HTTPException(404, "기준서를 찾을 수 없습니다.")
    if INDEX_STATUS.get(f"standards/{doc_id}", {}).get("status") == "running":
        raise HTTPException(409, "이미 인덱싱이 진행 중입니다.")
    background.add_task(_index_standard, doc_id)
    return {"status": "running", "doc_id": doc_id}


@app.get("/api/standards/index/status")
async def standards_index_status():
    return {k.split("/", 1)[1]: v for k, v in INDEX_STATUS.items()
            if k.startswith("standards/")}


@app.get("/api/standards/search")
async def standards_search(q: str, doc_ids: Optional[str] = None,
                           top_k: int = SEARCH_TOP_K):
    """선택(또는 전체) 회계기준서 본문 검색. 검색전용(LLM 생성 없음)·인용강제."""
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "질의가 비어 있습니다.")
    cat = load_standards_catalog()
    titles = {d.get("doc_id"): d.get("title") for d in cat["docs"]}
    want = set((doc_ids or "").split(",")) - {""}
    cells = []
    idx_by_doc: Dict[str, dict] = {}  # 스니펫 텍스트 보강용(로드한 인덱스 재활용)
    for d in cat["docs"]:
        did = d.get("doc_id")
        if (want and did not in want) or not d.get("indexed"):
            continue
        ip = standard_index_path(did)
        if not ip.exists():
            continue
        try:
            idx = json.loads(ip.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cells.append({"company": did, "period": "-", "index": idx})
        idx_by_doc[did] = idx
    if not cells:
        return {"query": q, "sources": [], "terms": [], "mode": "no_evidence"}
    try:
        q_emb = (await make_embedding(q)).tolist()
        sources = notes_rag.retrieve(
            q_emb, cells, fs_div="all", top_k=top_k, note_kind="전체",
            query_text=q, tokenize=tokenize_korean, expand=synonyms.expand_query,
            bm25_cls=(BM25Okapi if (USE_BM25 and _HAS_BM25) else None),
            cos_w=COS_W_DEFAULT, cos_w_policy=COS_W_POLICY)
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    if not sources:  # 인용강제 — 근거 없으면 결과 없음
        return {"query": q, "sources": [], "terms": [], "mode": "no_evidence"}
    q_terms = [t for t in dict.fromkeys(tokenize_korean(q)) if len(t) >= 2]

    def _unit_text(did, note_no, match_page):
        # _best_unit 은 최상위가 첫 청크면 text=None(제목 임베딩과 동점) → 인덱스에서 직접 보강.
        idx = idx_by_doc.get(did) or {}
        for n in idx.get("notes", []):
            if n.get("no") != note_no:
                continue
            chunks = n.get("chunks", [])
            for ch in chunks:  # 매칭 페이지 청크 우선
                if ch.get("page") == match_page and ch.get("text"):
                    return ch["text"]
            return chunks[0].get("text") if chunks else None
        return None

    def _snippet(text):
        t = re.sub(r"\s+", " ", (text or "").strip())
        if not t:
            return None
        hits = [t.find(term) for term in q_terms if t.find(term) >= 0]
        if hits:
            start = max(0, min(hits) - 20)
            return ("…" if start > 0 else "") + t[start:start + 100]
        return t[:100]
    src_out = []
    for s in sources:
        did = s.get("company")
        text = s.get("text") or _unit_text(did, s.get("note_no"), s.get("match_page"))
        src_out.append({"doc_id": did, "title": titles.get(did),
                        "note_no": s.get("note_no"), "page_start": s.get("page_start"),
                        "page_end": s.get("page_end"), "match_page": s.get("match_page"),
                        "score": s.get("score"), "snippet": _snippet(text)})
    return {"query": q, "sources": src_out, "terms": q_terms, "mode": "retrieval_only"}


@app.get("/api/standards/pdf")
async def standards_pdf(doc_id: str):
    """회계기준서 원본 PDF 스트리밍(inline, #page 이동 지원). doc_id 화이트리스트 검증."""
    if not _standard_exists(doc_id):
        raise HTTPException(404, "기준서를 찾을 수 없습니다.")
    target = standard_pdf_path(doc_id)
    if not target.exists():
        raise HTTPException(404, "PDF를 찾을 수 없습니다.")
    doc = next((d for d in load_standards_catalog()["docs"]
                if d.get("doc_id") == doc_id), {})
    orig = doc.get("filename_original")
    disposition = f"inline; filename*=UTF-8''{quote(orig)}" if orig else "inline"
    return FileResponse(target, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})


@app.delete("/api/standards/{doc_id}")
async def standards_delete(doc_id: str):
    if not _standard_exists(doc_id):
        raise HTTPException(404, "기준서를 찾을 수 없습니다.")
    d = standard_dir(doc_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    remove_standard(doc_id)
    INDEX_STATUS.pop(f"standards/{doc_id}", None)
    return {"deleted": True, "doc_id": doc_id}


# ----------------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    cat = load_catalog()
    backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "service": "4대 금융지주 주석 비교 에이전트 PoC v2",
        "embedding_backend": backend,
        "bm25_enabled": USE_BM25 and _HAS_BM25,
        "ollama_enabled": _OLLAMA_AVAILABLE,
        "openai_enabled": bool(OPENAI_API_KEY),
        "llm_provider": "openai" if OPENAI_API_KEY else ("ollama" if _OLLAMA_AVAILABLE else "none"),
        "ollama_model": OLLAMA_MODEL if _OLLAMA_AVAILABLE else None,
        "library_size": len(cat["entries"]),
    }


@app.get("/api/notes/account-refs")
async def notes_account_refs(company: str, period: str, fs_div: str = "연결", top_k: int = 2,
                             doc_type: Literal["review", "review_sep", "report"] = "review"):
    """재무제표 핵심계정 ↔ 주석 정합 참조 — 계정명 임베딩 ↔ note title 임베딩 cosine 상위.
    숫자 자동일치 금지(후보 제시·확정은 사용자). AI 무경유(임베딩 결정론)."""
    idx = _load_index(company, period, doc_type)
    if not idx:
        raise HTTPException(404, f"인덱스 없음: {company}/{period}")
    notes = [n for n in _notes_for_comparison(idx, fs_div) if "embedding" in n]
    out = []
    for aid, nm in fs_compare.TIMESERIES_ACCOUNTS:
        try:
            emb = (await make_embedding(nm)).tolist()
        except Exception as e:
            raise HTTPException(500, _safe_err(e))
        scored = sorted(
            ({"note_no": n["no"], "title": n["title"], "page_start": n.get("page_start"),
              "page_end": n.get("page_end"), "score": round(cosine(emb, n["embedding"]), 4)}
             for n in notes), key=lambda x: x["score"], reverse=True)
        out.append({"account_id": aid, "account_nm": nm, "matches": scored[:top_k]})
    return {"company": company, "period": period, "fs_div": fs_div, "rows": out}


@app.get("/api/notes/topic-map")
async def notes_topic_map(period: str, fs_div: str = "연결",
                          companies: Optional[str] = None, note_kind: str = "전체",
                          doc_type: Literal["review", "review_sep", "report"] = "review"):
    """§5.2 표준 주제 매핑 — 4사 주석을 canonical topic으로 분류·정렬(임베딩, AI 무경유)."""
    want = [c for c in (companies or "").split(",") if c] or list(VALID_COMPANIES)
    topics = _load_topic_dict_topics() or DEFAULT_TOPICS
    # 토픽 라벨 임베딩(결정론)
    try:
        topic_embs = {t: (await make_embedding(t)).tolist() for t in topics}
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    company_notes: Dict[str, list] = {}
    for c in want:
        idx = _load_index(c, period, doc_type)
        if not idx:
            continue
        notes = note_filters.filter_notes(_notes_for_comparison(idx, fs_div), note_kind)
        company_notes[c] = [n for n in notes if "embedding" in n]
    result = note_topics.build_topic_map(topic_embs, company_notes)
    result.update({"period": period, "fs_div": fs_div, "topic_min_score": note_topics.TOPIC_MIN_SCORE})
    return result


@app.get("/api/notes/compare-memo")
async def notes_compare_memo(topic: str, period: str, fs_div: str = "연결",
                             companies: Optional[str] = None, per_company: int = 1,
                             doc_type: Literal["review", "review_sep", "report"] = "review"):
    """§5.3 비교 메모 초안(옵트인) — 주제에 대한 4사 주석 정책·가정 차이 AI 초안.
    인용 강제: 근거(sources) 없으면 초안 생성 안 함. Ollama off=초안 없이 출처만."""
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "주제가 비어 있습니다.")
    want = [c for c in (companies or "").split(",") if c] or list(VALID_COMPANIES)
    try:
        t_emb = (await make_embedding(topic)).tolist()
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    # 회사별 주제 최근접 주석 top-N 수집(인용 출처)
    sources = []
    for c in want:
        idx = _load_index(c, period, doc_type)
        if not idx:
            continue
        cell = [{"company": c, "period": period, "index": idx}]
        got = notes_rag.retrieve(
            t_emb, cell, fs_div=fs_div, top_k=per_company,
            query_text=topic, tokenize=tokenize_korean, expand=synonyms.expand_query,
            bm25_cls=(BM25Okapi if (USE_BM25 and _HAS_BM25) else None),
            cos_w=COS_W_DEFAULT, cos_w_policy=COS_W_POLICY)
        for s in got:
            # 청크 본문 우선(정밀); 구 인덱스(text 없음)는 페이지 추출 폴백
            if not s.get("text"):
                s["text"] = notes_rag.extract_note_text(c, period, s.get("page_start"),
                                                        s.get("page_end"), doc_type=doc_type)
            sources.append(s)
    if not sources:
        return {"topic": topic, "period": period, "fs_div": fs_div,
                "sources": [], "memo": None, "mode": "no_evidence", "ollama": _OLLAMA_AVAILABLE}
    memo, mode = None, "retrieval_only"
    if _OLLAMA_AVAILABLE or OPENAI_API_KEY:
        try:
            memo = await notes_rag.answer_compare_ollama(topic, sources, OLLAMA_URL, OLLAMA_MODEL, openai_key=OPENAI_API_KEY)
            mode = "memo" if memo else "retrieval_only"
        except Exception as e:
            print(f"[compare-memo] 생성 실패(무시): {_safe_err(e)}", file=sys.stderr)
    src_out = [{k: s.get(k) for k in ("company", "period", "fs_div", "note_no",
                                      "title", "page_start", "page_end", "match_page", "score")} for s in sources]
    return {"topic": topic, "period": period, "fs_div": fs_div,
            "sources": src_out, "memo": memo, "mode": mode, "ollama": _OLLAMA_AVAILABLE}


# ----------------------------------------------------------------------------
# 주석 RAG (§5.4, 옵트인) — 정성 텍스트 전용. 숫자 무경유·출처 인용 강제·Ollama 옵트인.
# ----------------------------------------------------------------------------
@app.get("/api/notes/rag")
async def notes_rag_query(q: str, fs_div: str = "연결",
                          companies: Optional[str] = None,
                          period: Optional[str] = None, top_k: int = SEARCH_TOP_K,
                          note_kind: str = "전체", generate: bool = True,
                          include_body: bool = True,
                          cell_keys: Optional[str] = None,
                          doc_type: Literal["review", "review_sep", "report"] = "review"):
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "질의가 비어 있습니다.")

    def _merge_body(co, pe, dt, idx):
        # 전체 본문 유닛 병합(있으면) → 주석 외 본문(감사의견·재무제표 본표 등)도 검색.
        if include_body:
            bidx = _load_body_index(co, pe, dt)
            if bidx and bidx.get("notes"):
                return {**idx, "notes": list(idx.get("notes", [])) + bidx["notes"]}
        return idx

    # 인덱싱된 셀 수집(주석 소스). 숫자 아님 — 주석 텍스트만.
    cells = []
    cell_dt: Dict[tuple, str] = {}  # (회사,기간) → 문서유형 (출처 PDF·텍스트 폴백 해석용)
    if cell_keys:
        # 라이브러리에서 선택한 정확한 문서만 검색("회사~기간~문서유형" 목록). 연결/별도 혼재 가능.
        seen = set()
        for tok in cell_keys.split(","):
            parts = tok.split("~")
            if len(parts) != 3:
                continue
            co, pe, dt = (p.strip() for p in parts)
            if dt not in ("review", "review_sep") or (co, pe, dt) in seen:
                continue
            seen.add((co, pe, dt))
            idx = _load_index(co, pe, dt)
            if idx:
                cells.append({"company": co, "period": pe, "index": _merge_body(co, pe, dt, idx)})
                cell_dt[(co, pe)] = dt
        fs_div = "all"  # 선택 문서가 연결/별도 혼재 가능 → fs_div 필터 해제
    else:
        # report(사업보고서)는 연결·별도 혼재 → fs_div 필터를 우회(all)해 본문 전체 검색.
        if doc_type == "report":
            fs_div = "all"
        want_companies = set((companies or "").split(",")) - {""} or set(VALID_COMPANIES)
        for e in load_catalog()["entries"]:
            if e.get("company") in want_companies and _doc_indexed(e, doc_type):
                if period and e.get("period") != period:
                    continue
                idx = _load_index(e["company"], e["period"], doc_type)
                if idx:
                    cells.append({"company": e["company"], "period": e["period"],
                                  "index": _merge_body(e["company"], e["period"], doc_type, idx)})
                    cell_dt[(e["company"], e["period"])] = doc_type

    try:
        q_emb = (await make_embedding(q)).tolist()
        sources = notes_rag.retrieve(
            q_emb, cells, fs_div=fs_div, top_k=top_k, note_kind=note_kind,
            query_text=q, tokenize=tokenize_korean, expand=synonyms.expand_query,
            bm25_cls=(BM25Okapi if (USE_BM25 and _HAS_BM25) else None),
            cos_w=COS_W_DEFAULT, cos_w_policy=COS_W_POLICY)
        for s in sources:
            # 출처별 문서유형(선택 셀 혼재 대비) — 텍스트 폴백·PDF 링크 해석용.
            s["doc_type"] = cell_dt.get((s["company"], s["period"]), doc_type)
            # 청크 본문 우선(정밀 인용); 구 인덱스(text 없음)는 페이지 추출 폴백
            if not s.get("text"):
                s["text"] = notes_rag.extract_note_text(
                    s["company"], s["period"], s.get("page_start"), s.get("page_end"),
                    doc_type=s["doc_type"])
    except Exception as e:
        raise HTTPException(500, _safe_err(e))

    # 인용 강제(불변): 근거(sources) 없으면 답변 생성 안 함. Ollama off 면 retrieval_only.
    if not sources:
        return {"query": q, "fs_div": fs_div, "sources": [], "answer": None,
                "mode": "no_evidence", "ollama": _OLLAMA_AVAILABLE}
    answer = None
    mode = "retrieval_only"
    # generate=false: LLM 답변 생략(검색만) — 평가/디버깅용 고속 경로. 인용 강제는 유지.
    if generate and (_OLLAMA_AVAILABLE or OPENAI_API_KEY):
        try:
            answer = await notes_rag.answer_ollama(q, sources, OLLAMA_URL, OLLAMA_MODEL, openai_key=OPENAI_API_KEY)
            mode = "rag" if answer else "retrieval_only"
        except Exception as e:
            print(f"[notes_rag] LLM 생성 실패(무시): {_safe_err(e)}", file=sys.stderr)
    # 응답엔 본문 text 대신 출처 메타만 노출(원문 보호·경량화). answer 는 sources 동반 보장.
    # 출처 칩의 매칭 본문 샘플(≤100자). 매칭 용어(질의 형태소)는 프론트가 볼드 처리.
    q_terms = [t for t in dict.fromkeys(tokenize_korean(q)) if len(t) >= 2]

    def _snippet(s):
        t = re.sub(r"\s+", " ", (s.get("text") or "").strip())
        if not t:
            return None
        # 매칭 용어가 화면에 보이도록 첫 매칭 위치로 윈도우 시작(앞 20자 여유)
        hits = [t.find(term) for term in q_terms if t.find(term) >= 0]
        if hits:
            start = max(0, min(hits) - 20)
            return ("…" if start > 0 else "") + t[start:start + 100]
        return t[:100]
    src_out = [{**{k: s.get(k) for k in ("company", "period", "fs_div", "note_no", "doc_type",
                                         "title", "page_start", "page_end", "match_page", "score")},
                "snippet": _snippet(s)} for s in sources]
    return {"query": q, "fs_div": fs_div, "sources": src_out, "terms": q_terms,
            "answer": answer, "mode": mode, "ollama": _OLLAMA_AVAILABLE}


# 제목 스캔 셀 상한 — 경량 응답·결정론 보장(청크 미스캔, 제목만). 카탈로그 정렬 순회.
_SUGGEST_MAX_CELLS = 32


@app.get("/api/terms/suggest")
async def terms_suggest(q: str, companies: Optional[str] = None,
                        period: Optional[str] = None, fs_div: str = "연결",
                        doc_type: Literal["review", "review_sep", "report"] = "review"):
    """검색어 동의어/관련 용어 제안(오프라인·결정론·AI 무경유).

    안전경계: 로컬 어휘만 — synonyms 그룹 + 인덱싱된 주석 '제목'. 외부 API·임베딩·랭킹 무관여.
    제목만 스캔(청크 미접근)·셀 수 캡 → 경량. 비교조회·RAG 두 검색박스 공용.
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "질의가 비어 있습니다.")
    # 화이트리스트 교집합(CLAUDE.md 입력검증) — 미지/오타 회사명은 무시. 비면 전체.
    want_companies = (set((companies or "").split(",")) - {""}) & set(VALID_COMPANIES) or set(VALID_COMPANIES)

    # 제목 수집 — 인덱싱 셀의 notes 제목만(fs_div 필터). 결정론 위해 카탈로그 정렬 순회.
    note_titles: List[str] = []
    try:
        entries = sorted(
            (e for e in load_catalog()["entries"]
             if e.get("company") in want_companies and _doc_indexed(e, doc_type)
             and not (period and e.get("period") != period)),
            key=lambda e: (e.get("company", ""), e.get("period", "")),
        )
        for e in entries[:_SUGGEST_MAX_CELLS]:
            idx = _load_index(e["company"], e["period"], doc_type)
            if not idx:
                continue
            for n in _notes_for_comparison(idx, fs_div):
                title = (n.get("title") or "").strip()
                if title:
                    note_titles.append(title)
    except Exception as e:
        raise HTTPException(500, _safe_err(e))

    q_tokens = tokenize_korean(q)
    result = synonyms.suggest_terms(q, q_tokens, note_titles, synonyms.expand_query)
    return {"query": q, "applied": result["applied"], "suggestions": result["suggestions"]}


# 정적 파일 서빙
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
