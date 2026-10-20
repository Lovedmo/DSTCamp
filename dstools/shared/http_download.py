"""带多源切换、断点续传和 SHA-256 校验的文件下载（自动更新与按需下载的工具共用）。"""

from __future__ import annotations

import hashlib
import http.client
import os
import urllib.request
from pathlib import Path
from typing import Callable

from dstools.shared.ssl_context import default_ssl_context

ProgressCallback = Callable[[int, int], None]


def matches(path: Path, size: int, sha256: str) -> bool:
    """文件长度和 SHA-256 都与清单一致时才可复用。"""
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        return file_sha256(path) == sha256.lower()
    except OSError:
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _download_to_part(url: str, part: Path, total: int, progress: ProgressCallback | None) -> None:
    """把 url 下载到 part；part 已有半截时用 Range 续传。

    坑：Gitee 附件的 CDN 不支持 Range，会忽略请求头直接返回整份文件（200），这时必须从头覆盖写，
    不能把整份内容追加到半截后面；GitHub 支持（206）。
    """
    offset = part.stat().st_size if part.is_file() else 0
    if offset >= total:
        part.unlink()
        offset = 0
    headers = {"User-Agent": "DSTCamp"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30, context=default_ssl_context()) as response:
        if offset:
            content_range = str(response.headers.get("Content-Range") or "")
            if getattr(response, "status", 200) != 206 or not content_range.startswith(f"bytes {offset}-"):
                offset = 0
        downloaded = offset
        with part.open("ab" if offset else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if downloaded > total:
                    break
                if progress is not None:
                    progress(downloaded, total)
    if downloaded > total:
        part.unlink(missing_ok=True)
    if downloaded != total:
        raise OSError(f"下载大小不符：{downloaded} != {total}")


def download_verified(
    urls: tuple[str, ...], target: Path, size: int, sha256: str, progress: ProgressCallback | None
) -> None:
    """依次尝试各下载源，下到 target 并校验长度和 SHA-256；半截文件在源之间和程序重启后都能续传。"""
    temporary = target.with_name(target.name + ".part")
    last_error: Exception = OSError("没有可用的下载地址")
    for url in urls:
        try:
            _download_to_part(url, temporary, size, progress)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            continue
        if matches(temporary, size, sha256):
            os.replace(temporary, target)
            return
        # 内容不对（含续传拼出的坏文件）：丢掉半截，换下一个源从头下
        temporary.unlink(missing_ok=True)
        last_error = OSError("下载文件 SHA-256 校验失败")
    raise last_error
