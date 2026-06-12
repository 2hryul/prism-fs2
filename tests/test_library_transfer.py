"""인덱스 이식(내보내기/가져오기/rescan, 검색개선 Phase5b) 단위테스트.

디스크는 tmp_path 격리(LIBRARY_ROOT/CATALOG_PATH 몽키패치), 네트워크 없음.
"""
import sys
import io
import json
import asyncio
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(app, "LIBRARY_ROOT", root)
    monkeypatch.setattr(app, "CATALOG_PATH", tmp_path / "catalog.json")
    return root


def _seed_cell(root, company="신한", period="2025Q3", indexed=True):
    d = root / company / period
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.pdf").write_bytes(b"%PDF-1.4")
    (d / "fs_structured.json").write_text("{}", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"rcept_no": "R1", "report_nm": "분기보고서 (2025.09)"},
        ensure_ascii=False), encoding="utf-8")
    if indexed:
        (d / "index.json").write_text(json.dumps({
            "schema": 3, "source_type": "full_report", "detected_unit": "원",
            "notes": [{"no": 1, "title": "회사의 개요", "fs_div": "연결"},
                      {"no": 1, "title": "일반사항", "fs_div": "별도"}],
        }, ensure_ascii=False), encoding="utf-8")
    return d


# ── _cell_entry_from_disk / rescan ───────────────────────────────────────────
def test_cell_entry_from_disk(lib):
    _seed_cell(lib)
    e = app._cell_entry_from_disk("신한", "2025Q3")
    assert e["report_collected"] is True and e["fs_collected"] is True
    assert e["indexed"] is True and e["notes_count"] == 2
    assert e["notes_count_연결"] == 1 and e["notes_count_별도"] == 1
    assert e["rcept_no"] == "R1"


def test_cell_entry_empty_dir_returns_none(lib):
    (lib / "KB" / "2025Q2").mkdir(parents=True)
    assert app._cell_entry_from_disk("KB", "2025Q2") is None


def test_rescan_rebuilds_catalog(lib):
    _seed_cell(lib, "신한", "2025Q3")
    _seed_cell(lib, "KB", "2025Q2", indexed=False)
    res = asyncio.run(app.rescan_library())
    assert res["cells"] == 2
    cat = app.load_catalog()
    by = {(e["company"], e["period"]): e for e in cat["entries"]}
    assert by[("신한", "2025Q3")]["indexed"] is True
    assert by[("KB", "2025Q2")].get("indexed") is not True
    assert by[("KB", "2025Q2")]["report_collected"] is True


# ── zip 멤버 검증(경로 탈출 차단) ────────────────────────────────────────────
def test_validate_zip_member():
    assert app._validate_zip_member("신한/2025Q3/index.json") == ("신한", "2025Q3")
    assert app._validate_zip_member("manifest.json") is None        # 루트 파일
    assert app._validate_zip_member("../../evil.exe") is None       # 경로 탈출
    assert app._validate_zip_member("신한/2025Q3/../../x") is None
    assert app._validate_zip_member("C:/windows/x") is None         # 절대/드라이브
    assert app._validate_zip_member("도둑/2025Q3/a.json") is None   # 미등록 회사
    assert app._validate_zip_member("신한/9999XX/a.json") is None   # 비정상 기간


# ── import 코어 ──────────────────────────────────────────────────────────────
def _make_export_zip(tmp_path, dim=None, cells=("신한/2025Q3",)):
    dim = dim if dim is not None else app.EMBED_DIM
    zp = tmp_path / "export.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "app": "prism-fs", "schema": 3, "embed_dim": dim, "cells": list(cells)}))
        for c in cells:
            zf.writestr(f"{c}/index.json", json.dumps({
                "schema": 3, "notes": [{"no": 1, "title": "회사의 개요", "fs_div": "연결"}]},
                ensure_ascii=False))
            zf.writestr(f"{c}/report.pdf", "%PDF-1.4")
    return zp


def test_import_zip_merges_and_registers(lib, tmp_path):
    zp = _make_export_zip(tmp_path)
    res = app._import_zip_blocking(zp, overwrite=False)
    assert res["imported"] == ["신한/2025Q3"]
    assert (lib / "신한" / "2025Q3" / "index.json").exists()
    cat = app.load_catalog()
    assert cat["entries"][0]["indexed"] is True  # 카탈로그 자동 등록


def test_import_skips_existing_without_overwrite(lib, tmp_path):
    _seed_cell(lib, "신한", "2025Q3")
    zp = _make_export_zip(tmp_path)
    res = app._import_zip_blocking(zp, overwrite=False)
    assert res["skipped"] == ["신한/2025Q3"] and res["imported"] == []


def test_import_dim_mismatch_rejected(lib, tmp_path):
    """차원 불일치 반입 선제 차단 — bigram(512) 인덱스를 768 환경에 못 들임."""
    zp = _make_export_zip(tmp_path, dim=512 if app.EMBED_DIM != 512 else 768)
    with pytest.raises(HTTPException) as ei:
        app._import_zip_blocking(zp, overwrite=False)
    assert "차원" in ei.value.detail


def test_import_without_manifest_rejected(lib, tmp_path):
    zp = tmp_path / "bad.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("신한/2025Q3/index.json", "{}")
    with pytest.raises(HTTPException):
        app._import_zip_blocking(zp, overwrite=False)