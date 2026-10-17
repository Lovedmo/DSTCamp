"""通过 SteamCMD 安装/更新独立专服（不依赖界面）。

真机实测（中文 Windows）：
- 清单写在 force_install_dir 下的 steamapps/appmanifest_343050.acf，专服文件直接放在 force_install_dir；
- 输出跟随系统语言（中文系统输出中文），不能按文本判断成败，只看退出码和清单；
- 刚装好的 SteamCMD 首次 app_update 报 "Missing configuration"（退出码 7），所以失败后自动重试一次。
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from dstools.features.local_service.steam_client_updater import (
    DEDICATED_SERVER_APP_ID, SteamAppSnapshot, snapshot_from_manifest,
)
from dstools.shared import app_settings

_LINE_SPLIT_RE = re.compile(rb"[\r\n]+")


def is_steamcmd_exe(path: Path | None) -> bool:
    return bool(path) and Path(path).name.casefold() == "steamcmd.exe" and Path(path).is_file()


def find_steamcmd() -> Path | None:
    """用户选过且仍然存在的 steamcmd.exe。"""
    path = app_settings.get_steamcmd_path()
    return path if is_steamcmd_exe(path) else None


def is_steam_library_install(install_dir: Path) -> bool:
    """装在 Steam 库 steamapps/common 下的由 Steam 客户端管理，其余按 SteamCMD 安装处理。"""
    parent = Path(install_dir).parent
    return parent.name.casefold() == "common" and parent.parent.name.casefold() == "steamapps"


def snapshot_install(install_dir: Path, app_id: str = DEDICATED_SERVER_APP_ID) -> SteamAppSnapshot:
    """读 SteamCMD 安装目录自己的清单；没有清单时只凭专服文件判断是否已安装。"""
    install_dir = Path(install_dir)
    manifest = install_dir / "steamapps" / f"appmanifest_{app_id}.acf"
    existing = install_dir if install_dir.is_dir() else None
    if not manifest.is_file():
        return SteamAppSnapshot(app_id, None, existing, None, None, None, None, None)
    return snapshot_from_manifest(app_id, manifest, existing)


def build_command(steamcmd: Path, install_dir: Path, *, validate: bool = True) -> list[str]:
    # force_install_dir 必须写在 login 之前，否则 SteamCMD 会提示并忽略
    command = [str(steamcmd), "+force_install_dir", str(install_dir),
               "+login", "anonymous", "+app_update", DEDICATED_SERVER_APP_ID]
    if validate:
        command.append("validate")
    command.append("+quit")
    return command


def update_succeeded(returncode: int, snapshot: SteamAppSnapshot) -> bool:
    return (returncode == 0 and snapshot.manifest_path is not None and snapshot.install_ready
            and snapshot.download_complete and not snapshot.update_pending)


def _run_once(command: list[str], on_line: Callable[[str], None],
              cancel_event: threading.Event | None) -> int:
    proc = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    finished = threading.Event()

    def watch_cancel() -> None:
        while not finished.is_set():
            if cancel_event.wait(0.5):
                proc.kill()
                return

    if cancel_event is not None:
        threading.Thread(target=watch_cancel, daemon=True).start()
    pending = b""
    last = None
    try:
        while chunk := proc.stdout.read1(4096):
            # 进度行用 \r 覆盖刷新，按 \r/\n 一起切行；连续重复的进度行只报一次
            parts = _LINE_SPLIT_RE.split(pending + chunk)
            pending = parts.pop()
            for raw in parts:
                text = raw.decode("utf-8", errors="replace").strip()
                if text and text != last:
                    last = text
                    on_line(text)
        tail = pending.decode("utf-8", errors="replace").strip()
        if tail and tail != last:
            on_line(tail)
        return proc.wait()
    finally:
        finished.set()
        proc.stdout.close()


def run_update(
    steamcmd: Path,
    install_dir: Path,
    *,
    on_line: Callable[[str], None],
    on_retry: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    validate: bool = True,
    attempts: int = 2,
) -> SteamAppSnapshot:
    """运行 SteamCMD 直到清单显示安装完整；全部失败抛 RuntimeError，取消抛 InterruptedError。"""
    command = build_command(steamcmd, install_dir, validate=validate)
    returncode = None
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1 and on_retry is not None:
            on_retry(returncode)
        returncode = _run_once(command, on_line, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("SteamCMD 更新已取消")
        snapshot = snapshot_install(install_dir)
        if update_succeeded(returncode, snapshot):
            return snapshot
    raise RuntimeError(f"SteamCMD 退出码 {returncode}")
