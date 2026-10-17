"""SteamCMD 通道下工坊 Mod 的状态（不依赖界面）：SteamCMD 没有订阅和自动更新，状态只看本地清单与 Web API。

与 workshop_status.py（Steam 客户端通道，依赖 Steam API）互不混用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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


def inspect_steamcmd_mods(workshop_ids) -> dict[str, SteamcmdModStatus]:
    """对照 SteamCMD 工坊目录与 Web API 给出状态；网络错误直接抛出。"""
    ids = [str(w) for w in dict.fromkeys(workshop_ids) if str(w).isdigit()]
    content_root = steamcmd_workshop_dir() / "content" / WORKSHOP_APP_ID
    times = read_acf_time_updated(content_root)
    installed = {wid: times.get(int(wid), 0) for wid in ids if has_workshop_files(content_root / wid)}
    return classify(ids, installed, fetch_remote_items(ids))
