"""SteamCMD 通道下工坊 Mod 的状态（不依赖界面）：SteamCMD 没有订阅和自动更新，状态只看本地清单与 Web API。

与 workshop_status.py（Steam 客户端通道，依赖 Steam API）互不混用。
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class SteamcmdModStatus:
    workshop_id: str
    state: SteamcmdModState
    title: str = ""

    @property
    def needs_download(self) -> bool:
        return self.state in (SteamcmdModState.MISSING, SteamcmdModState.OUTDATED)


def classify(workshop_ids, installed: dict[str, int], remote: dict[str, RemoteWorkshopItem]) -> dict[str, SteamcmdModStatus]:
    """installed 为已有完整文件的项目在清单里的更新时间。工坊查不到的一律算不可用，不猜测已下载的是否最新。"""
    result = {}
    for wid in dict.fromkeys(str(w) for w in workshop_ids):
        item = remote.get(wid)
        if item is None:
            state = SteamcmdModState.UNAVAILABLE
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
    """全局更新列表的一行：状态、格式（V1 只有 *_legacy.bin、V2 有 modinfo.lua，没下载时为 None）与使用它的存档。"""
    status: SteamcmdModStatus
    name: str = ""
    mod_format: str | None = None
    used_by: tuple[str, ...] = ()
    v1_deploy_pending: bool = False  # V1 包已下载，但专服 mods 里的展开副本不存在或与包不一致（启动前自动部署）

    @property
    def workshop_id(self) -> str:
        return self.status.workshop_id


def scan_steamcmd_mods(clusters, server_mods_root: Path | None) -> list[SteamcmdModRow]:
    """SteamCMD 全局更新的候选：它的工坊目录里已下载的 Mod，加上任一 Steam 服务器存档列着的（含还没下载的）。
    状态对照 Web API（网络错误直接抛出）；V1 另查专服 mods 里的展开副本是否与包一致。"""
    from dstools.features.mod.legacy_v1 import find_legacy_packages, legacy_runtime_matches_package
    from dstools.features.mod.parser import detect_mod_format
    from dstools.features.mod.steamcmd_mod_cleanup import read_mod_name, workshop_ids_by_cluster

    content_root = steamcmd_workshop_dir() / "content" / WORKSHOP_APP_ID
    used = workshop_ids_by_cluster(clusters)
    try:
        downloaded = [p.name for p in content_root.iterdir() if p.is_dir() and p.name.isdigit()]
    except OSError:
        downloaded = []
    ids = sorted(set(downloaded) | set(used), key=int)
    statuses = inspect_steamcmd_mods(ids)
    packages = find_legacy_packages()  # SteamCMD 通道下即它的工坊目录
    rows = []
    for wid in ids:
        status = statuses[wid]
        mod_format = detect_mod_format(wid, content_root) if has_workshop_files(content_root / wid) else None
        pending = False
        package = packages.get(int(wid))
        if mod_format == "V1" and package is not None and server_mods_root is not None:
            pending = not legacy_runtime_matches_package(package, Path(server_mods_root) / f"workshop-{wid}")
        name = status.title or (read_mod_name(content_root / wid) if mod_format == "V2" else "")
        rows.append(SteamcmdModRow(status, name, mod_format, tuple(used.get(wid, ())), pending))
    return rows


def inspect_steamcmd_mods(workshop_ids) -> dict[str, SteamcmdModStatus]:
    """对照 SteamCMD 工坊目录与 Web API 给出状态；网络错误直接抛出。"""
    ids = [str(w) for w in dict.fromkeys(workshop_ids) if str(w).isdigit()]
    content_root = steamcmd_workshop_dir() / "content" / WORKSHOP_APP_ID
    times = read_acf_time_updated(content_root)
    installed = {wid: times.get(int(wid), 0) for wid in ids if has_workshop_files(content_root / wid)}
    return classify(ids, installed, fetch_remote_items(ids))
