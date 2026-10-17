"""通过 SteamCMD 安装/更新独立专服（不依赖界面）。

真机实测（中文 Windows）：
- 清单写在 force_install_dir 下的 steamapps/appmanifest_343050.acf，专服文件直接放在 force_install_dir；
- 输出跟随系统语言（中文系统输出中文），不能按文本判断成败，只看退出码和清单；
- 刚装好的 SteamCMD 首次 app_update 报 "Missing configuration"（退出码 7），所以失败后自动重试一次；
- 标准输出接管道时是块缓冲，进程结束才一次性吐出，所以运行中改为跟踪它实时写入的 logs/content_log.txt；
- 账号登录时密码从标准输入读取（不回显）；没有缓存凭据又不给输入时 2 秒内失败，密码错误同样退出码 5。
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from dstools.features.local_service.steam_client_updater import (
    DEDICATED_SERVER_APP_ID, SteamAppSnapshot, snapshot_from_manifest,
)
from dstools.shared import app_settings
from dstools.shared.resource_paths import cache_dir, path_is_ascii, tool_binary_dir

_LINE_SPLIT_RE = re.compile(rb"[\r\n]+")
# 实测：密码错误、缓存凭据失效都是退出码 5。登录失败不自动重试，避免连续失败触发 Steam 的登录限制
LOGIN_FAILED_EXIT_CODE = 5


class SteamcmdLoginError(RuntimeError):
    """账号登录失败或缓存的登录凭据已失效，需要重新登录。"""


def is_steamcmd_exe(path: Path | None) -> bool:
    return bool(path) and Path(path).name.casefold() == "steamcmd.exe" and Path(path).is_file()


def bundled_steamcmd_path() -> Path:
    """内置 SteamCMD 的运行位置：缓存目录下（缓存目录要求纯 ASCII，SteamCMD 在非 ASCII 路径下直接报错退出）。
    放缓存而不是 data：程序和它自更新下载的组件都可重建，清空缓存后下次使用重新释放、重新登录即可。"""
    return cache_dir("steamcmd") / "steamcmd.exe"


def ensure_bundled_steamcmd() -> Path | None:
    """首次使用时把随包的官方 steamcmd.exe 原样释放到缓存目录；已存在就不动（SteamCMD 会自更新这个文件）。
    路径含非 ASCII 字符或释放失败时返回 None。"""
    target = bundled_steamcmd_path()
    if not path_is_ascii(target):
        return None
    if target.is_file():
        return target
    source = tool_binary_dir() / "steamcmd"
    try:
        data = gzip.decompress((source / "steamcmd.exe.gz").read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        # 许可协议要求随程序附带，原样放在旁边
        shutil.copyfile(source / "LICENSE.txt", target.parent / "LICENSE.txt")
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return target


def steamcmd_workshop_dir() -> Path:
    """SteamCMD 下载工坊内容的位置（steamcmd.exe 同级 steamapps/workshop，不受 force_install_dir 影响）。
    实测：目录结构、appworkshop_322330.acf 与 Steam 客户端相同，下载内容逐字节一致。只算路径，不释放程序。"""
    custom = custom_steamcmd_path()
    root = custom.parent if custom is not None else bundled_steamcmd_path().parent
    return root / "steamapps" / "workshop"


def custom_steamcmd_path() -> Path | None:
    """用户改选的 steamcmd.exe；没选过返回 None（使用内置）。"""
    return app_settings.get_steamcmd_path()


def find_steamcmd() -> Path | None:
    """实际使用的 steamcmd.exe：用户改选过就用它（不存在或路径含非 ASCII 字符时返回 None，不偷偷换内置），
    否则用内置的。"""
    custom = custom_steamcmd_path()
    if custom is not None:
        return custom if is_steamcmd_exe(custom) and path_is_ascii(custom) else None
    return ensure_bundled_steamcmd()


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


def build_command(steamcmd: Path, install_dir: Path, *, validate: bool = True, account: str = "") -> list[str]:
    """account 为空时匿名登录；有账号时只传用户名，靠 SteamCMD 缓存的凭据登录。"""
    # force_install_dir 必须写在 login 之前，否则 SteamCMD 会提示并忽略
    command = [str(steamcmd), "+force_install_dir", str(install_dir),
               "+login", account or "anonymous", "+app_update", DEDICATED_SERVER_APP_ID]
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


def is_workshop_log_line(line: str) -> bool:
    """workshop_log 里只转发每个项目的开始与结果（实测格式：Download item <ID> requested / result : OK）。"""
    return "Download item" in line


def _follow_log(log_path: Path, accept: Callable[[str], bool], on_line: Callable[[str], None],
                stop: threading.Event) -> None:
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
                    if line and accept(line):
                        on_line(line)
        except OSError:
            pass
        if stopping:
            return


def _run_once(command: list[str], on_line: Callable[[str], None],
              cancel_event: threading.Event | None, stdin_text: str | None = None,
              log_name: str = "content_log.txt",
              log_filter: Callable[[str], bool] = is_progress_log_line) -> int:
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
        target=_follow_log,
        args=(Path(command[0]).parent / "logs" / log_name, log_filter, on_line, log_stop), daemon=True)
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
    account: str = "",
) -> SteamAppSnapshot:
    """运行 SteamCMD 直到清单显示安装完整；全部失败抛 RuntimeError，账号登录失败抛 SteamcmdLoginError，
    取消抛 InterruptedError。"""
    command = build_command(steamcmd, install_dir, validate=validate, account=account)
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
        if account and returncode == LOGIN_FAILED_EXIT_CODE:
            raise SteamcmdLoginError(f"SteamCMD 退出码 {returncode}")
    raise RuntimeError(f"SteamCMD 退出码 {returncode}")


WORKSHOP_APP_ID = "322330"
_WORKSHOP_BATCH = 50  # 每次 SteamCMD 下载的项目数，控制命令行长度，也让中途失败只影响一批


def has_workshop_files(content_dir: Path) -> bool:
    """V2 解压后有 modinfo.lua，V1 是 *_legacy.bin（启动前由 DSTCamp 部署）。"""
    try:
        return (content_dir / "modinfo.lua").is_file() or any(
            item.is_file() for item in content_dir.glob("*_legacy.bin"))
    except OSError:
        return False


def download_workshop_items(
    steamcmd: Path,
    workshop_ids,
    *,
    on_line: Callable[[str], None],
    account: str = "",
    cancel_event: threading.Event | None = None,
) -> dict[str, int]:
    """用 SteamCMD 下载/更新工坊 Mod 到它自己的工坊目录，返回 {工坊 ID: 清单里的更新时间}，没装上的为 0。

    成败按 appworkshop_322330.acf 与实际文件判断，不看输出文字；是否已是最新由调用方对照 Web API。
    账号登录失效时抛 SteamcmdLoginError，取消抛 InterruptedError。
    """
    from dstools.features.mod.workshop_acf import read_acf_time_updated

    ids = [str(wid) for wid in dict.fromkeys(workshop_ids) if str(wid).isdigit()]
    content_root = Path(steamcmd).parent / "steamapps" / "workshop" / "content" / WORKSHOP_APP_ID
    for start in range(0, len(ids), _WORKSHOP_BATCH):
        command = [str(steamcmd), "+login", account or "anonymous"]
        for wid in ids[start:start + _WORKSHOP_BATCH]:
            command += ["+workshop_download_item", WORKSHOP_APP_ID, wid]
        command.append("+quit")
        returncode = _run_once(command, on_line, cancel_event,
                               log_name="workshop_log.txt", log_filter=is_workshop_log_line)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("SteamCMD 下载已取消")
        if account and returncode == LOGIN_FAILED_EXIT_CODE:
            raise SteamcmdLoginError(f"SteamCMD 退出码 {returncode}")
    installed = read_acf_time_updated(content_root)
    return {wid: installed.get(int(wid), 0) if has_workshop_files(content_root / wid) else 0 for wid in ids}


def needs_guard_code(lines: list[str]) -> bool:
    """登录输出里出现 Steam 令牌验证码提示（登录相关输出是英文，真机实测）。"""
    return any("Steam Guard code" in line or "Account Logon Denied" in line for line in lines)


def login(
    steamcmd: Path,
    username: str,
    password: str,
    guard_code: str = "",
    *,
    on_line: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> int:
    """用账号密码登录一次，成功后 SteamCMD 会缓存凭据，之后只传用户名即可；返回退出码（0 为成功）。

    密码和令牌验证码经标准输入传入，不出现在命令行里，也不转发到输出。
    """
    secrets = [value for value in (password, guard_code) if value]

    def safe_line(line: str) -> None:
        if not any(secret in line for secret in secrets):
            on_line(line)

    stdin_text = password + "\n" + (guard_code + "\n" if guard_code else "")
    returncode = _run_once([str(steamcmd), "+login", username, "+quit"], safe_line, cancel_event,
                           stdin_text=stdin_text)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("SteamCMD 登录已取消")
    return returncode
