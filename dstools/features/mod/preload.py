"""启动后在后台预热已安装 Mod 的磁盘缓存：modinfo 解析、图标 PNG、作者声明的版本号。

新用户首次打开 Mod 页时，每个 Mod 都要转一次图标、起一个沙箱子进程读版本，几百个 Mod 要几十秒；
预热后 Mod 页、新建存档向导都直接命中缓存。只写缓存，不碰存档与界面，也不发布到 ModCatalogStore
（不带图标的快照会让新建存档向导显示无图标列表，而为预热把图标读进内存又白占几十 MB）。
缺 VC++ 2013 运行库时只跳过图标转换（tex_to_png 自己判断），列表、名称和版本照常预热。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dstools.features.mod.icons import get_mod_icon_path
from dstools.features.mod.local_version import resolve_local_version_target
from dstools.features.mod.modinfo_cache import parse_modinfo_cached
from dstools.features.mod.parser import find_mod_folder, list_installed_mod_ids
from dstools.models import Platform

# 沙箱每个 Mod 起一个子进程；比 Mod 页的 4 路并发低，避免刚启动时抢满 CPU、引来杀软集中扫描
VERSION_WORKERS = 2


@dataclass
class PreloadStats:
    mods: int = 0
    icons: int = 0
    versions: int = 0
    cancelled: bool = False


def warm_mod_caches(
    platform: Platform,
    wegame_client_mods_dir: Path | None = None,
    steam_runtime_mods_dir: Path | None = None,
    stop: threading.Event | None = None,
) -> PreloadStats:
    """按与 Mod 页相同的目录规则枚举 Mod 并逐个填缓存；stop 置位后尽快返回。V1 Legacy 包不预热（需临时解压）。"""
    stop = stop or threading.Event()
    stats = PreloadStats()
    targets = []
    for wid in list_installed_mod_ids(platform, wegame_client_mods_dir, steam_runtime_mods_dir=steam_runtime_mods_dir):
        if stop.is_set():
            stats.cancelled = True
            return stats
        try:
            folder = find_mod_folder(wid, platform, wegame_client_mods_dir, steam_runtime_mods_dir)
            info = parse_modinfo_cached(folder) if folder else None
        except Exception:
            continue
        if info is not None:
            targets.append((wid, folder, info))
    stats.mods = len(targets)

    # 图标先做：单次转换只要零点几秒，用户最先看到的就是图标
    for wid, folder, info in targets:
        if stop.is_set():
            stats.cancelled = True
            return stats
        try:
            if get_mod_icon_path(info, folder, platform) is not None:
                stats.icons += 1
        except Exception:
            continue

    def resolve(target) -> bool:
        if stop.is_set():
            return False
        try:
            resolve_local_version_target(target)
        except Exception:
            return False
        return True

    version_targets = [(wid, folder, info.workshop_id) for wid, folder, info in targets]
    with ThreadPoolExecutor(max_workers=VERSION_WORKERS) as pool:
        stats.versions = sum(pool.map(resolve, version_targets))
    stats.cancelled = stop.is_set()
    return stats
