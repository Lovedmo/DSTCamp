"""LuaJIT 补丁依赖的 VC++ 2015-2022 x64 运行库：检测、从微软官网下载、验签和安装。

配套 Mod 的 Injector.dll / Winmm.dll 依赖 x64 的 VCRUNTIME140(_1).dll 与 MSVCP140.dll。
官网固定链接始终指向最新版，大小和哈希会变，不能像 tool_store 那样写死 SHA-256，
改为校验 Authenticode 签名有效且签名者为 Microsoft Corporation。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable

from dstools.shared.resource_paths import cache_dir
from dstools.shared.ssl_context import default_ssl_context

DOWNLOAD_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
_REGISTRY_KEY = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
# 读不到 DLL 链接器版本时的下限：VS 2022 17.10 起编译的 STL 要求运行库不低于 14.40
_FALLBACK_REQUIRED = (14, 40)
# 安装程序退出码：成功、成功但需重启、已装有更新的版本
_INSTALL_OK_CODES = (0, 3010, 1638)
_INSTALL_CANCELLED_CODES = (1602, 1223)
_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

ProgressCallback = Callable[[int, int], None]


def installed_version() -> tuple[int, int] | None:
    """注册表里登记的 x64 运行库 (主, 次) 版本；未安装返回 None。"""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REGISTRY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            if not winreg.QueryValueEx(key, "Installed")[0]:
                return None
            return int(winreg.QueryValueEx(key, "Major")[0]), int(winreg.QueryValueEx(key, "Minor")[0])
    except (OSError, ValueError):
        return None


def linker_version(dll: Path) -> tuple[int, int] | None:
    """读 PE 可选头里的链接器版本（MSVC 工具集版本，如 14.44）。"""
    try:
        with dll.open("rb") as source:
            header = source.read(4096)
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if header[pe_offset:pe_offset + 4] != b"PE\0\0":
            return None
        # 可选头紧跟 20 字节的 COFF 头，开头 2 字节是 Magic，随后是主、次链接器版本
        major, minor = struct.unpack_from("<BB", header, pe_offset + 24 + 2)
        return major, minor
    except (OSError, struct.error):
        return None


def required_version(dlls: list[Path]) -> tuple[int, int]:
    """运行库不能比编译这些 DLL 的工具集旧；取其中最高的链接器版本。"""
    versions = [v for v in map(linker_version, dlls) if v is not None and v[0] == 14]
    return max(versions, default=_FALLBACK_REQUIRED)


def is_satisfied(dlls: list[Path]) -> bool:
    installed = installed_version()
    return installed is not None and installed >= required_version(dlls)


def _installer_path() -> Path:
    return cache_dir("vcredist") / "vc_redist.x64.exe"


def download(progress: ProgressCallback | None = None) -> Path:
    """从微软官网下载安装程序到缓存目录并验签，返回路径；失败抛 OSError。"""
    target = _installer_path()
    temporary = target.with_name(target.name + ".part")
    request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "DSTCamp"})
    with urllib.request.urlopen(request, timeout=30, context=default_ssl_context()) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    if total and done != total:
        temporary.unlink(missing_ok=True)
        raise OSError(f"下载大小不符：{done} != {total}")
    verify_signature(temporary)
    os.replace(temporary, target)
    return target


def verify_signature(path: Path) -> None:
    """要求文件内嵌的 Authenticode 签名有效且签名者为 Microsoft Corporation，否则删除文件并抛 OSError。

    坑：系统自带程序靠目录（Catalog）签名，复制改名后同样显示有效，所以必须要求内嵌签名。"""
    # 路径经环境变量传入，避免在命令行里拼接引号
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:DSTCAMP_VERIFY_PATH; "
              "Write-Output ([string]$s.Status); Write-Output ([string]$s.SignatureType); "
              "Write-Output ([string]$s.SignerCertificate.Subject)")
    env = dict(os.environ, DSTCAMP_VERIFY_PATH=str(path))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, env=env, creationflags=_CREATIONFLAGS)
        lines = result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        path.unlink(missing_ok=True)
        raise OSError(f"无法校验数字签名：{exc}") from exc
    status, kind, subject = (lines + ["", "", ""])[:3]
    if status.strip() != "Valid" or kind.strip() != "Authenticode" or "O=Microsoft Corporation" not in subject:
        path.unlink(missing_ok=True)
        raise OSError(f"数字签名校验未通过：{status.strip() or '无签名'} {kind.strip()} {subject}".strip())


def install(installer: Path) -> None:
    """显示进度条静默安装（会弹出一次 UAC），等待结束；失败抛 OSError，用户取消抛 InterruptedError。"""
    try:
        code = subprocess.run([str(installer), "/install", "/passive", "/norestart"],
                              cwd=str(installer.parent), timeout=900).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"无法运行安装程序：{exc}") from exc
    if code in _INSTALL_CANCELLED_CODES:
        raise InterruptedError
    if code not in _INSTALL_OK_CODES:
        raise OSError(f"安装程序退出码 {code}")
    installer.unlink(missing_ok=True)
