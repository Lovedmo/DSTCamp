"""Mod 作者声明版本号的可信沙箱结果缓存。"""

import hashlib
import json
from pathlib import Path
from typing import Any

from dstools.features.mod.sandbox import VERSION_CONTRACT_VERSION
from dstools.shared.resource_paths import cache_dir

_CACHE_DIR = cache_dir("mod_versions")
_CACHE_FORMAT_VERSION = 1


def _cache_path(workshop_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in workshop_id)
    return _CACHE_DIR / f"{safe_id}.json"


def _fingerprint(modinfo_path: Path) -> str | None:
    try:
        return hashlib.sha256(modinfo_path.read_bytes()).hexdigest()
    except OSError:
        return None


def load_version_result(workshop_id: str, modinfo_path: Path,
                        folder_name: str) -> dict[str, Any] | None:
    """仅在内容、来源路径、folder_name 和沙箱协议完全一致时命中。"""
    fingerprint = _fingerprint(modinfo_path)
    if fingerprint is None:
        return None
    try:
        source_path = str(modinfo_path.resolve())
    except OSError:
        return None
    return _load(workshop_id, fingerprint, source_path, folder_name)


def save_version_result(workshop_id: str, modinfo_path: Path, folder_name: str,
                        result: dict[str, Any]) -> None:
    """保存一次成功执行的结果；失败不缓存，以便环境变化后自然重试。"""
    fingerprint = _fingerprint(modinfo_path)
    if fingerprint is None:
        return
    try:
        source_path = str(modinfo_path.resolve())
    except OSError:
        return
    _save(workshop_id, fingerprint, source_path, folder_name, result)


def _package_cache_key(workshop_id: str) -> str:
    # 与同 ID 的 V2 目录缓存分开存，避免两者互相覆盖
    return f"legacy-{workshop_id}"


def load_package_version_result(workshop_id: str, archive_path: Path, modinfo_bytes: bytes,
                                folder_name: str) -> dict[str, Any] | None:
    """V1 Legacy 包内 modinfo.lua 的版本结果；来源路径记压缩包路径，内容哈希取包内 modinfo.lua。"""
    try:
        source_path = str(Path(archive_path).resolve())
    except OSError:
        return None
    return _load(_package_cache_key(workshop_id), hashlib.sha256(modinfo_bytes).hexdigest(),
                 source_path, folder_name)


def save_package_version_result(workshop_id: str, archive_path: Path, modinfo_bytes: bytes,
                                folder_name: str, result: dict[str, Any]) -> None:
    try:
        source_path = str(Path(archive_path).resolve())
    except OSError:
        return
    _save(_package_cache_key(workshop_id), hashlib.sha256(modinfo_bytes).hexdigest(),
          source_path, folder_name, result)


def _load(cache_key: str, fingerprint: str, source_path: str, folder_name: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(_cache_path(cache_key).read_text(encoding="utf-8"))
        if raw.get("cache_format") != _CACHE_FORMAT_VERSION:
            return None
        if raw.get("contract_version") != VERSION_CONTRACT_VERSION:
            return None
        if raw.get("sha256") != fingerprint:
            return None
        if raw.get("source_path") != source_path:
            return None
        if raw.get("folder_name") != folder_name:
            return None
        result = raw.get("result")
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _save(cache_key: str, fingerprint: str, source_path: str, folder_name: str,
          result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        return
    payload = {
        "cache_format": _CACHE_FORMAT_VERSION,
        "contract_version": VERSION_CONTRACT_VERSION,
        "sha256": fingerprint,
        "source_path": source_path,
        "folder_name": folder_name,
        "result": result,
    }
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(cache_key).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
