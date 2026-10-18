"""SteamCMD 通道下工坊 Mod 的状态（不依赖界面）：SteamCMD 没有订阅和自动更新，状态只看本地清单与 Web API。

与 workshop_status.py（Steam 客户端通道，依赖 Steam API）互不混用。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from dstools.features.local_service.steamcmd_updater import (
    WORKSHOP_APP_ID, has_workshop_files, steamcmd_workshop_dir,
)
from dstools.features.mod.workshop_acf import read_acf_time_updated
from dstools.features.mod.workshop_webapi import RemoteWorkshopItem, fetch_remote_items


class SteamcmdModState(str, Enum):
    MISSING = "missing"          # 没下载过（或文件不全），可以下载
    OUTDATED = "outdated"        # 已下载，工坊上有更新
    CURRENT = "current"          # 已下载且与工坊一致
    UNAVAILABLE = "unavailable"  # 工坊查不到（已删除、私密或 ID 无效）
    CLIENT_ONLY = "client_only"  # 仅客户端 Mod（工坊标签 client_only_mod），专服不加载，不用下载和更新


@dataclass(frozen=True)
class SteamcmdModStatus:
    workshop_id: str
    state: SteamcmdModState
    title: str = ""

    @property
    def needs_download(self) -> bool:
        return self.state in (SteamcmdModState.MISSING, SteamcmdModState.OUTDATED)


def classify(workshop_ids, installed: dict[str, int], remote: dict[str, RemoteWorkshopItem],
             needed_ids=frozenset()) -> dict[str, SteamcmdModStatus]:
    """installed 为已有完整文件的项目在清单里的更新时间。工坊查不到的一律算不可用，不猜测已下载的是否最新。
    仅客户端 Mod 专服不加载，不下载也不更新；needed_ids 里的（如 LuaJIT 配套 Mod，DSTCamp 要用它的文件）照常处理。"""
    result = {}
    for wid in dict.fromkeys(str(w) for w in workshop_ids):
        item = remote.get(wid)
        if item is None:
            state = SteamcmdModState.UNAVAILABLE
        elif item.client_only and wid not in needed_ids:
            state = SteamcmdModState.CLIENT_ONLY
        elif wid not in installed:
            state = SteamcmdModState.MISSING
        elif installed[wid] < item.time_updated:
            state = SteamcmdModState.OUTDATED
        else:
            state = SteamcmdModState.CURRENT
        result[wid] = SteamcmdModStatus(wid, state, item.title if item else "")
    return result


@dataclass(frozen=True)
class SteamcmdModRow:
    """全局更新列表的一行：格式（V1 只有 *_legacy.bin、V2 有 modinfo.lua，没下载时为 None）、使用它的存档，
    以及对照 Web API 的状态（还没查到时为 None）。"""
    workshop_id: str
    name: str = ""
    mod_format: str | None = None
    used_by: tuple[str, ...] = ()
    v1_deploy_pending: bool = False  # V1 包已下载，但专服 mods 里的展开副本不存在或与包不一致（启动前自动部署）
    status: SteamcmdModStatus | None = None

    @property
    def needs_download(self) -> bool:
        return self.status is not None and self.status.needs_download


def collect_steamcmd_mod_rows(clusters, server_mods_root: Path | None) -> list[SteamcmdModRow]:
    """SteamCMD 全局更新的候选（只读本地，不联网）：它的工坊目录里已下载的 Mod，加上任一 Steam 服务器存档列着的
    （含还没下载的）；V1 另查专服 mods 里的展开副本是否与包一致。状态由 with_statuses 补上。"""
    from dstools.features.mod.legacy_v1 import find_legacy_packages, legacy_runtime_matches_package
    from dstools.features.mod.parser import detect_mod_format
    from dstools.features.mod.steamcmd_mod_cleanup import read_mod_name, workshop_ids_by_cluster

    content_root = steamcmd_workshop_dir() / "content" / WORKSHOP_APP_ID
    used = workshop_ids_by_cluster(clusters)
    try:
        downloaded = [p.name for p in content_root.iterdir() if p.is_dir() and p.name.isdigit()]
    except OSError:
        downloaded = []
    packages = find_legacy_packages()  # SteamCMD 通道下即它的工坊目录
    rows = []
    for wid in sorted(set(downloaded) | set(used), key=int):
        mod_format = detect_mod_format(wid, content_root) if has_workshop_files(content_root / wid) else None
        pending = False
        package = packages.get(int(wid))
        if mod_format == "V1" and package is not None and server_mods_root is not None:
            pending = not legacy_runtime_matches_package(package, Path(server_mods_root) / f"workshop-{wid}")
        name = read_mod_name(content_root / wid) if mod_format == "V2" else ""
        rows.append(SteamcmdModRow(wid, name, mod_format, tuple(used.get(wid, ())), pending))
    return rows


def with_statuses(rows: list[SteamcmdModRow], statuses: dict[str, SteamcmdModStatus]) -> list[SteamcmdModRow]:
    """给各行填上 Web API 状态；本地读不出名字的用工坊标题。"""
    result = []
    for row in rows:
        status = statuses.get(row.workshop_id)
        name = row.name or (status.title if status is not None else "")
        result.append(replace(row, status=status, name=name))
    return result


def inspect_steamcmd_mods(workshop_ids) -> dict[str, SteamcmdModStatus]:
    """对照 SteamCMD 工坊目录与 Web API 给出状态；网络错误直接抛出。"""
    ids = [str(w) for w in dict.fromkeys(workshop_ids) if str(w).isdigit()]
    content_root = steamcmd_workshop_dir() / "content" / WORKSHOP_APP_ID
    times = read_acf_time_updated(content_root)
    installed = {wid: times.get(int(wid), 0) for wid in ids if has_workshop_files(content_root / wid)}
    # 延迟导入：luajit_injector 依赖 Mod 解析模块
    from dstools.features.local_service.luajit_injector import WORKSHOP_ID as LUAJIT_WORKSHOP_ID
    return classify(ids, installed, fetch_remote_items(ids), needed_ids={LUAJIT_WORKSHOP_ID})
