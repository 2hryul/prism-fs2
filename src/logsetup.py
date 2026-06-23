"""파일 로깅 설정 — stdout/stderr 를 콘솔과 회전 로그파일에 동시 기록(Tee).

이 프로젝트는 logging 모듈 대신 print(..., file=sys.stderr) 를 광범위하게 쓰므로,
개별 logger 설정으로는 로그를 다 잡지 못한다. 따라서 스트림 자체를 Tee 하여
uvicorn 액세스/에러 로그·print·트레이스백을 빠짐없이 파일에도 남긴다.

- 위치(Windows): %LOCALAPPDATA%\prism-fs\logs\prism-fs_YYYYMMDD.log
  (LOCALAPPDATA 부재 환경: ~/.prism-fs/logs)
- 보관: 30일 초과 로그 자동 삭제(기동 시 1회 정리).
- 보안: 외부 호출 없음. 민감정보(DART 키 등)는 호출부에서 이미 마스킹/미출력하므로
  스트림에 흐르지 않는다(키는 .env 에서만 읽고 type(e).__name__ 만 로깅).
- frozen(콘솔 숨김) 실행에서도 로그를 남겨 사후 진단을 가능하게 하는 것이 목적.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

APP_NAME = "prism-fs"
LOG_RETENTION_DAYS = 30
_installed = False  # 여러 진입점에서 호출돼도 스트림은 1회만 래핑(중복 방지)


def log_dir() -> Path:
    """로그 디렉터리 경로. Windows=%LOCALAPPDATA%\\prism-fs\\logs, 그 외=~/.prism-fs/logs."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME / "logs"
    return Path.home() / f".{APP_NAME}" / "logs"


def _cleanup_old_logs(d: Path, retention_days: int = LOG_RETENTION_DAYS) -> int:
    """보관기간(기본 30일) 초과 로그파일 삭제. 삭제 건수 반환(실패는 무시).

    파일 수정시각(mtime) 기준. 디렉터리 접근/삭제 실패는 조용히 건너뛴다(로깅이
    본 기능을 깨지 않도록).
    """
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    try:
        candidates = list(d.glob(f"{APP_NAME}_*.log"))
    except OSError:
        return 0
    for f in candidates:
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass  # 사용 중/권한 문제 — 건너뜀
    return removed


class _Tee:
    """원본 스트림 + 로그파일에 동시 기록하는 래퍼.

    파일 쓰기 실패는 무시하고 콘솔 출력은 유지한다(로깅이 앱을 죽이지 않도록).
    원본이 None(windowed stream 부재)이면 파일에만 기록한다.
    """

    def __init__(self, original, logfile):
        self._original = original
        self._logfile = logfile

    def write(self, data):
        written = 0
        if self._original is not None:
            try:
                written = self._original.write(data) or 0
            except Exception:
                pass
        try:
            self._logfile.write(data)
            self._logfile.flush()
        except Exception:
            pass
        return written or len(data)

    def flush(self):
        for s in (self._original, self._logfile):
            try:
                if s is not None:
                    s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._original) and self._original.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        # write/flush/isatty 외 속성은 원본 스트림에 위임(uvicorn 호환).
        return getattr(self._original, name)


def setup_file_logging() -> Optional[Path]:
    """stdout/stderr 를 콘솔+오늘자 로그파일에 Tee. 활성화된 로그 경로 반환(실패 시 None).

    멱등: 이미 설치됐으면 아무것도 하지 않고 None 반환. 디렉터리/파일 생성 실패 시에도
    예외 없이 None 을 돌려 콘솔 전용으로 진행한다(기동을 막지 않음).
    """
    global _installed
    if _installed:
        return None
    d = log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[logsetup] 로그 디렉터리 생성 실패 - {d} ({type(e).__name__}) — 콘솔 전용 진행",
              file=sys.stderr)
        return None
    _cleanup_old_logs(d)
    path = d / f"{APP_NAME}_{datetime.now().strftime('%Y%m%d')}.log"
    try:
        logfile = open(path, "a", encoding="utf-8", buffering=1)  # 라인 버퍼링(즉시 기록)
    except OSError as e:
        print(f"[logsetup] 로그 파일 열기 실패 - {path} ({type(e).__name__}) — 콘솔 전용 진행",
              file=sys.stderr)
        return None
    try:
        logfile.write(f"\n==== {APP_NAME} 로그 시작 "
                      f"{datetime.now().isoformat(timespec='seconds')} (pid={os.getpid()}) ====\n")
        logfile.flush()
    except Exception:
        pass
    sys.stdout = _Tee(sys.stdout, logfile)
    sys.stderr = _Tee(sys.stderr, logfile)
    _installed = True
    return path
