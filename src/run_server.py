"""
run_server.py — 데스크톱 진입점 (PyInstaller onedir 번들)

폐쇄망 데모 PC 에서 더블클릭 실행 → 로컬 uvicorn(:8021) 기동 후 기본 브라우저 자동 오픈.
오프라인 강제(HF/Transformers 네트워크 시도 0). storage·모델은 paths 모듈이 frozen 인지 해석.

개발 모드에서도 동일하게 동작: python src/run_server.py
"""
import os
import sys
import threading
import webbrowser

# 한글 Windows 콘솔(cp949)은 em-dash(—)·화살표(→) 등을 인코딩 못 해 기동 로그 출력 시
# UnicodeEncodeError 로 죽는다(데모 PC 재현). 진입점에서 stdout/stderr 를 UTF-8 로 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # windowed 모드 등 stream 부재 — 무시

# 임베딩 모델 오프라인 강제(번들 동봉 모델만 사용 — 외부 다운로드 시도 차단).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("USE_OLLAMA", "auto")  # Ollama 가동 시 자동 사용, 없으면 비활성

# 파일 로깅(콘솔+회전 로그파일 Tee) — 더블클릭(콘솔 숨김) 실행에서도 사후 진단 가능.
# app import 전에 설치해 기동/임포트 시점 로그·트레이스백까지 파일에 남긴다.
try:
    from logsetup import setup_file_logging  # noqa: E402
    _LOG_PATH = setup_file_logging()
    if _LOG_PATH:
        print(f"[startup] 로그 파일: {_LOG_PATH}", file=sys.stderr)
except Exception as _e:  # 로깅 실패가 기동을 막지 않도록 방어
    print(f"[startup] 파일 로깅 설정 실패(무시) - {type(_e).__name__}", file=sys.stderr)

import uvicorn  # noqa: E402
from app import app  # noqa: E402  — paths 가 frozen storage/static/model 경로 해석

HOST, PORT = "127.0.0.1", 8021


def _open_browser():
    """서버가 뜬 직후 기본 브라우저로 대시보드 오픈(약간의 기동 지연 후)."""
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Timer(2.5, _open_browser).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
