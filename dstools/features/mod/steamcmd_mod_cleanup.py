"""清理 SteamCMD 工坊目录里不再使用的 Mod（不依赖界面）。

只处理 SteamCMD 自己的工坊目录，Steam 客户端订阅的 Mod 一律不碰（那边要靠取消订阅，直接删会被重新下载）。
"在用"指任一 Steam 服务器存档的任一世界 modoverrides.lua 里列着（启用或停用都算，免得停用后配置还在、
再启用时又要重下）。实测：只删内容目录、不改 appworkshop_322330.acf，之后 SteamCMD 照常重新下载。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from dstools.features.mod.manager import load_mod_overrides
from dstools.models import Platform, SaveSource

_APP_ID = "322330"
# 有的作者在名字里放游戏图标字体的私用区字符（实测 Uncompromising Mode），界面上显示成方块
_PRIVATE_USE_RE = re.compile("[\ue000-\uf8ff\U000f0000-\U0010ffff]")


@dataclass(frozen=True)
class SteamcmdModEntry:
    workshop_id: str
    path: Path
    size: int
    name: str = ""


def workshop_ids_by_cluster(clusters) -> dict[str, list[str]]:
    """{工坊 ID（纯数字）: 列着它的 Steam 服务器存档名}，按存档顺序；任一世界 modoverrides.lua 列着即算。"""
    result: dict[str, list[str]] = {}
    for cluster in clusters:
        if cluster.source is not SaveSource.SERVER or cluster.platform is not Platform.STEAM:
            continue
        for shard in cluster.shards:
            if not shard.mod_overrides_path:
                continue
            for entry in load_mod_overrides(shard.mod_overrides_path).mods.values():
                wid = str(entry.workshop_id).replace("workshop-", "")
                if wid.isdigit() and cluster.name not in result.setdefault(wid, []):
                    result[wid].append(cluster.name)
    return result


def used_workshop_ids(clusters) -> set[str]:
    """所有 Steam 服务器存档各世界 modoverrides.lua 里列着的工坊 ID（纯数字）。"""
    return set(workshop_ids_by_cluster(clusters))


def _dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def read_mod_name(path: Path) -> str:
    from dstools.features.mod.parser import parse_modinfo

    try:
        info = parse_modinfo(path)
    except Exception:  # modinfo 写法千奇百怪，读不出名字只影响显示
        return ""
    return _PRIVATE_USE_RE.sub("", info.name or "").strip() if info else ""


def _numbered_dirs(root: Path) -> list[Path]:
    try:
        return sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return []


def find_unused_mods(workshop_root: Path, used_ids: set[str]) -> list[SteamcmdModEntry]:
    """SteamCMD 工坊目录里不在 used_ids 中的 Mod，按占用空间从大到小。"""
    entries = [
        SteamcmdModEntry(path.name, path, _dir_size(path), read_mod_name(path))
        for path in _numbered_dirs(Path(workshop_root) / "content" / _APP_ID)
        if path.name not in used_ids
    ]
    return sorted(entries, key=lambda e: e.size, reverse=True)


def find_leftover_downloads(workshop_root: Path) -> list[SteamcmdModEntry]:
    """取消或中断下载后残留在 downloads/、temp/ 下的半成品目录。"""
    root = Path(workshop_root)
    return [
        SteamcmdModEntry(path.name, path, _dir_size(path))
        for sub in ("downloads", "temp")
        for path in _numbered_dirs(root / sub / _APP_ID)
    ]


def _is_deletable(path: Path, workshop_root: Path) -> bool:
    """只允许删工坊目录下 content|downloads|temp/322330/<纯数字> 这一层，联接一律不删。"""
    path = Path(path)
    if not path.name.isdigit() or path.parent.name != _APP_ID or path.parent.parent.name not in (
            "content", "downloads", "temp"):
        return False
    try:
        if os.path.isjunction(path) or path.is_symlink():
            return False
        return path.parent.parent.parent.resolve() == Path(workshop_root).resolve()
    except OSError:
        return False


def delete_mod_dirs(paths, workshop_root: Path) -> list[tuple[Path, str]]:
    """删除选中的目录，返回失败的 [(路径, 原因)]；不在允许范围内的路径拒绝删除。"""
    failures = []
    for path in paths:
        path = Path(path)
        if not _is_deletable(path, workshop_root):
            failures.append((path, "不在 SteamCMD 工坊目录的 Mod 位置，已拒绝删除"))
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            failures.append((path, str(exc)))
    return failures
