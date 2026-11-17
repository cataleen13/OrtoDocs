# -*- mode: python ; coding: utf-8 -*-
"""Configurație PyInstaller (onedir, windowed) pentru OrtoDocs."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


SPEC_DIR = Path(SPECPATH).resolve()
ROOT_DIR = SPEC_DIR.parent
PYPROJECT_PATH = ROOT_DIR / "pyproject.toml"
VERSION_INFO_PATH = SPEC_DIR / "version_info.txt"
MANIFEST_PATH = SPEC_DIR / "ortodocs.manifest"
ICON_PATH = SPEC_DIR / "ortodocs.ico"
LICENSES_DIR = SPEC_DIR / "licenses"
NOTICES_FILE = SPEC_DIR / "THIRD_PARTY_LICENSES.txt"


def _read_canonical_version() -> str:
    with PYPROJECT_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"Versiune invalidă în {PYPROJECT_PATH}.")
    return version.strip()


def _validate_version_info(app_version: str) -> None:
    parts = [int(part) for part in app_version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    version_tuple = tuple(parts[:4])
    content = VERSION_INFO_PATH.read_text(encoding="utf-8")
    expected_tuple = f"filevers={version_tuple!r}"
    expected_product = f"StringStruct('ProductVersion', '{app_version}')"
    if expected_tuple not in content or expected_product not in content:
        raise ValueError(
            f"{VERSION_INFO_PATH} nu este sincronizat cu versiunea {app_version} din {PYPROJECT_PATH}."
        )


APP_VERSION = _read_canonical_version()
_validate_version_info(APP_VERSION)

datas: list[tuple[str, str]] = []
datas.extend(collect_data_files("docx"))
datas.append((str(PYPROJECT_PATH), "."))
datas.append((str(ICON_PATH), "."))
datas.append((str(NOTICES_FILE), "licenses"))
datas.append((str(LICENSES_DIR), "licenses"))

try:
    datas.extend(copy_metadata("ortodocs"))
except importlib.metadata.PackageNotFoundError:
    pass


a = Analysis(
    [str(ROOT_DIR / "src" / "ortodocs" / "main.py")],
    pathex=[str(ROOT_DIR / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tests",
        "pip",
        "setuptools",
        "wheel",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OrtoDocs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH),
    manifest=str(MANIFEST_PATH),
    version=str(VERSION_INFO_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OrtoDocs",
)
