"""通过 SteamCMD 安装/更新独立专服（不依赖界面）。

真机实测（中文 Windows）：
- 清单写在 force_install_dir 下的 steamapps/appmanifest_343050.acf，专服文件直接放在 force_install_dir；
- 输出跟随系统语言（中文系统输出中文），不能按文本判断成败，只看退出码和清单；
- 刚装好的 SteamCMD 首次 app_update 报 "Missing configuration"（退出码 7），所以失败后自动重试一次；
- 标准输出接管道时是块缓冲，进程结束才一次性吐出，所以运行中改为跟踪它实时写入的 logs/content_log.txt。
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


def is_progress_log_line(line: str, app_id: str = DEDICATED_SERVER_APP_ID) -> bool:
    """content_log 里只转发本 App 的状态变化、下载速率和失败原因，下载源、连接细节不转发。"""
    return f"AppID {app_id}" in line or "download rate" in line or "Failed" in line


def _follow_content_log(log_path: Path, on_line: Callable[[str], None], stop: threading.Event) -> None:
    """从启动时的末尾开始跟踪新写入的行；文件被轮转变短时从头读。"""
    try:
        offset = log_path.stat().st_size
    except OSError:
        offset = 0
    pending = ""
    while True:
        stopping = stop.wait(1.0)
        try:
            size = log_path.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with log_path.open("rb") as file:
                    file.seek(offset)
                    data = file.read(size - offset)
                offset = size
                lines = (pending + data.decode("utf-8", errors="replace")).split("\n")
                pending = lines.pop()
                for line in lines:
                    line = line.strip()
                    if line and is_progress_log_line(line):
                        on_line(line)
        except OSError:
            pass
        if stopping:
            return


def _run_once(command: list[str], on_line: Callable[[str], None],
              cancel_event: threading.Event | None, stdin_text: str | None = None) -> int:
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if stdin_text is not None:
        # 密码等交互输入预先写入：提示同样被缓冲，没法等看到提示再输入
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    finished = threading.Event()
    log_stop = threading.Event()
    log_thread = threading.Thread(
        target=_follow_content_log,
        args=(Path(command[0]).parent / "logs" / "content_log.txt", on_line, log_stop), daemon=True)
    log_thread.start()

    def watch_cancel() -> None:
        while not finished.is_set():
            if cancel_event.wait(0.5):
                proc.kill()
                return

    if cancel_event is not None:
        threading.Thread(target=watch_cancel, daemon=True).start()
    try:
        output = b""
        while chunk := proc.stdout.read1(4096):
            output += chunk
        returncode = proc.wait()
    finally:
        finished.set()
        log_stop.set()
        log_thread.join(3)
        proc.stdout.close()
    # 进度行用 \r 覆盖刷新，按 \r/\n 一起切行；连续重复的行只报一次
    last = None
    for raw in _LINE_SPLIT_RE.split(output):
        text = raw.decode("utf-8", errors="replace").strip()
        if text and text != last:
            last = text
            on_line(text)
    return returncode


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
