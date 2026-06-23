"""파일 로깅(logsetup) 단위테스트 — 경로 해석·Tee 동작·보관정리·멱등 설치.

setup_file_logging 은 전역 sys.stdout/stderr 를 래핑하므로, 테스트는 반드시
원복하고 _installed 플래그를 리셋해 pytest 캡처를 오염시키지 않는다.
"""
import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import logsetup  # noqa: E402


def test_log_dir_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = logsetup.log_dir()
    assert d == tmp_path / "prism-fs" / "logs"


def test_log_dir_fallback_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    d = logsetup.log_dir()
    assert d.name == "logs" and ".prism-fs" in str(d)


def test_tee_writes_to_both(tmp_path):
    original = io.StringIO()
    logfile = open(tmp_path / "t.log", "w+", encoding="utf-8")
    try:
        tee = logsetup._Tee(original, logfile)
        tee.write("안녕 hello\n")
        tee.flush()
        assert original.getvalue() == "안녕 hello\n"
        logfile.seek(0)
        assert logfile.read() == "안녕 hello\n"
    finally:
        logfile.close()


def test_tee_survives_none_original(tmp_path):
    """원본 스트림이 None(windowed)이어도 파일에는 기록."""
    logfile = open(tmp_path / "t.log", "w+", encoding="utf-8")
    try:
        tee = logsetup._Tee(None, logfile)
        n = tee.write("x")
        tee.flush()
        logfile.seek(0)
        assert logfile.read() == "x" and n == 1
        assert tee.isatty() is False
    finally:
        logfile.close()


def test_cleanup_old_logs(tmp_path):
    old = tmp_path / "prism-fs_20200101.log"
    new = tmp_path / "prism-fs_20991231.log"
    other = tmp_path / "keepme.txt"
    for f in (old, new, other):
        f.write_text("x", encoding="utf-8")
    # old 의 mtime 을 보관기간 한참 이전으로 조작
    past = time.time() - 60 * 86400
    os.utime(old, (past, past))
    removed = logsetup._cleanup_old_logs(tmp_path, retention_days=30)
    assert removed == 1
    assert not old.exists()       # 30일 초과 → 삭제
    assert new.exists()           # 최신 → 보존
    assert other.exists()         # 패턴 불일치(.txt) → 손대지 않음


def test_setup_file_logging_creates_file_and_tees(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    saved_out, saved_err = sys.stdout, sys.stderr
    logsetup._installed = False
    try:
        path = logsetup.setup_file_logging()
        assert path is not None and path.exists()
        # 설치 후 stdout 은 Tee 로 래핑
        assert isinstance(sys.stdout, logsetup._Tee)
        print("진단 메시지 diagnostic-line")
        sys.stdout.flush()
        content = path.read_text(encoding="utf-8")
        assert "diagnostic-line" in content
        assert "로그 시작" in content  # 배너
        # 멱등: 두 번째 호출은 None
        assert logsetup.setup_file_logging() is None
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        logsetup._installed = False
