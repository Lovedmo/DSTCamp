"""Steam 内容的更新方式（唯一来源）：Steam 客户端或 SteamCMD，由用户在「更新方式」里明确切换。

专服更新（以及后续的 Mod 更新）都按这里的选择走，两条路径互不混用，不会因为找不到一种就偷偷换另一种。
"""

from __future__ import annotations

from enum import Enum

from dstools.features.local_service import steamcmd_updater
from dstools.features.local_service.server_runtime import ServerRuntime
from dstools.shared import app_settings


class SteamBackend(str, Enum):
    CLIENT = "client"
    STEAMCMD = "steamcmd"


def get_backend() -> SteamBackend:
    try:
        return SteamBackend(app_settings.get_steam_backend() or SteamBackend.CLIENT.value)
    except ValueError:
        return SteamBackend.CLIENT


def set_backend(backend: SteamBackend) -> None:
    app_settings.set_steam_backend(SteamBackend(backend).value)


def update_blocker(backend: SteamBackend, runtime: ServerRuntime | None) -> str | None:
    """当前更新方式不能更新这个开服程序时返回原因文案键，可以更新返回 None。"""
    if backend is SteamBackend.STEAMCMD:
        if steamcmd_updater.find_steamcmd() is None:
            return "local.backend_steamcmd_missing"
        if runtime is None:
            return "local.backend_steamcmd_no_runtime"
        if runtime.is_client:
            # 游戏本体只能由拥有游戏的账号更新，匿名 SteamCMD 不行
            return "local.backend_steamcmd_client_runtime"
        if steamcmd_updater.is_steam_library_install(runtime.install_dir):
            # 同一份文件不交给两个程序管理，避免 Steam 客户端和 SteamCMD 的清单互相打架
            return "local.backend_steamcmd_library_dir"
        return None
    if runtime is not None and not runtime.is_client \
            and not steamcmd_updater.is_steam_library_install(runtime.install_dir):
        return "local.backend_client_steamcmd_dir"
    return None
