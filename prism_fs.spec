# -*- mode: python ; coding: utf-8 -*-
"""
prism_fs.spec — PyInstaller onedir 풀번들 (prism-fs)

torch + sentence-transformers + ko-sroberta 모델을 함께 묶어 폐쇄망에서 개발환경과
동일한 AI 검색·주제매핑·RAG 동작을 보장한다(인덱스 768차원 ↔ bigram 512차원 불일치 회피).
산출물 이름은 VERSION(PRISM_VERSION 환경변수) 으로 자동 주입: setup_v{VERSION}.

빌드: build.ps1 또는 직접 `pyinstaller prism_fs.spec --distpath dist\\setup --workpath build\\work`
"""
import os
from PyInstaller.utils.hooks import collect_all

VERSION = os.environ.get("PRISM_VERSION", "0.1.0")
NAME = f"setup_v{VERSION}"

# 번들 동봉 리소스: 정적 UI(vendored pdf.js 포함) + 임베딩 모델.
datas = [
    ("src/static", "static"),
    ("src/models/ko-sroberta", "models/ko-sroberta"),
]
binaries = []
# 앱 모듈 + uvicorn 런타임 서브모듈(동적 import 라 명시 필요).
hiddenimports = [
    "app", "paths", "safety", "fs_compare", "notes_rag",
    "note_filters", "note_topics", "collect_dart", "synonyms",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

# 무거운 패키지는 데이터·바이너리·서브모듈 일괄 수집(누락 시 frozen 기동 실패 방지).
# numpy/pandas 명시 수집 필수 — numpy 2.x 는 서브모듈(numpy._core._exceptions 등)이
# 자동 추적되지 않아 frozen 기동 시 ModuleNotFoundError 발생(슬림 venv 에서 재현).
for pkg in ("numpy", "pandas", "sentence_transformers", "transformers", "torch",
            "tokenizers", "safetensors", "fitz", "rank_bm25", "sklearn", "scipy",
            "huggingface_hub", "fastapi", "starlette", "kiwipiepy", "kiwipiepy_model"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # 미설치 패키지는 건너뜀(예: sklearn/scipy 선택적)

a = Analysis(
    ["src/run_server.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 미사용·번들 비대 패키지 제외(빌드 venv엔 애초에 미설치 — 이중 안전장치).
    #   cv2/av/camelot: 표 추출(camelot) 전이 의존성이나 앱 미사용(PyMuPDF 사용).
    #   torch CUDA: 빌드 venv가 CPU torch라 해당 없음. test/notebook 류 개발 전용.
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "notebook", "IPython",
              "cv2", "av", "camelot", "pypdf", "pytest", "playwright"],
    noarchive=False,
    # transformers/sentence_transformers 는 지연 로딩(_LazyModule)이 __file__ 로
    # 소스를 다시 찾는다 → PYZ 바이트코드(.pyc)만 있으면 WinError 3(소스 누락)로
    # 모델 로드 실패→bigram 폴백. 소스(.py) 모드로 디스크에 펼쳐 __file__ 정합.
    module_collection_mode={
        "transformers": "py",
        "sentence_transformers": "py",
    },
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 폐쇄망 로그 가시성 유지
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
