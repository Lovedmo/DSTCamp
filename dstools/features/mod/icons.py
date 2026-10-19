"""解析并缓存每个 Mod 的图标（Klei atlas：.tex 贴图 + UV 的 .xml）。

每个 Mod 只转换裁剪一次，按 workshop id 落盘缓存，避免反复调用 ktech。缓存旁记一份源文件指纹
（.tex/.xml 的路径、修改时间、大小与图标名），完全一致才命中：同步脚本会保留源文件的旧时间，
只比较"缓存是否更新"会把换过的图标当成没变。
"""

import json
import os
import stat
import threading
from pathlib import Path

from PIL import Image

from dstools.shared.atlas_utils import crop_by_uv, parse_atlas_xml
from dstools.features.mod.parser import ModInfo
from dstools.shared.resource_paths import cache_dir
from dstools.shared.tex_convert import tex_to_png
from dstools.models import Platform


# 进程内解码尺寸上限：列表实际图标边长约 100px，限制在 192px 以免 512/960px 图标长期占用内存（磁盘仍存原图）
MAX_RESIDENT_ICON_SIZE = 192


def load_mod_icon_image(
    path: Path, max_size: int = MAX_RESIDENT_ICON_SIZE
) -> Image.Image:
    """读取界面用的 RGBA 图标并限制进程内驻留尺寸（磁盘缓存保留原始转换结果）。"""
    with Image.open(path) as source:
        image = source.convert("RGBA")
    if max(image.size) > max_size:
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return image


def _cache_dir_for(platform: Platform) -> Path:
    """按平台分开的缓存子目录（Steam/WeGame 是独立目录树，同 ID 也可能是不同 Mod）。"""
    return cache_dir("mod_icons") / platform.value


def _source_stamp(mod_info: ModInfo, xml_path: Path, tex_path: Path) -> tuple[list, int] | None:
    """源 .xml/.tex 的指纹与 .tex 的修改时间；任一不是文件时返回 None。

    每次加载 Mod 页都要对全部 Mod 调用：只 stat 一次，不用 resolve()（Windows 上很慢，路径本来就是绝对的）。"""
    try:
        xml_stat, tex_stat = xml_path.stat(), tex_path.stat()
    except OSError:
        return None
    if not (stat.S_ISREG(xml_stat.st_mode) and stat.S_ISREG(tex_stat.st_mode)):
        return None
    stamp = [mod_info.icon, [str(xml_path), xml_stat.st_mtime_ns, xml_stat.st_size],
             [str(tex_path), tex_stat.st_mtime_ns, tex_stat.st_size]]
    return stamp, tex_stat.st_mtime


def _write_stamp(cache_path: Path, stamp: list) -> None:
    try:
        cache_path.with_suffix(".json").write_text(json.dumps(stamp, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def get_cached_mod_icon_path(
    mod_info: ModInfo,
    mod_folder: Path,
    platform: Platform = Platform.STEAM,
) -> Path | None:
    """只返回仍与源 .tex 匹配的已有 PNG，不做转换（首帧快速恢复），缺失或失效返回 None 交给后台转换。"""
    if not mod_info.icon or not mod_info.icon_atlas:
        return None
    xml_path = Path(mod_folder) / mod_info.icon_atlas
    source = _source_stamp(mod_info, xml_path, xml_path.parent / mod_info.icon)
    if source is None:
        return None
    stamp, tex_mtime = source
    cache_path = _cache_dir_for(platform) / f"{mod_info.workshop_id}.png"
    try:
        if json.loads(cache_path.with_suffix(".json").read_text(encoding="utf-8")) != stamp:
            return None
        return cache_path if cache_path.is_file() else None
    except (OSError, ValueError):
        pass
    # 没有指纹的旧缓存按原规则（不比源 .tex 旧）接管一次并补记指纹，免得升级后全部重新转换
    try:
        if cache_path.stat().st_mtime >= tex_mtime:
            _write_stamp(cache_path, stamp)
            return cache_path
    except OSError:
        pass
    return None


def get_mod_icon_path(mod_info: ModInfo, mod_folder: Path,
                       platform: Platform = Platform.STEAM) -> Path | None:
    """返回 Mod 图标的缓存 PNG 路径，首次使用时转换；没有图标或转换失败返回 None（调用方用占位图）。"""
    cached = get_cached_mod_icon_path(mod_info, mod_folder, platform)
    if cached is not None:
        return cached
    if not mod_info.icon or not mod_info.icon_atlas:
        return None

    xml_path = mod_folder / mod_info.icon_atlas
    tex_path = xml_path.parent / mod_info.icon
    if not xml_path.exists() or not tex_path.exists():
        return None

    cache_dir_path = _cache_dir_for(platform)
    cache_path = cache_dir_path / f"{mod_info.workshop_id}.png"
    # 转换前取指纹：转换中途源文件又变了，下次照样失效；转换失败时旧指纹不匹配，下次继续重试
    source = _source_stamp(mod_info, xml_path, tex_path)

    cache_dir_path.mkdir(parents=True, exist_ok=True)
    atlas_png = cache_dir_path / f"_atlas_{mod_info.workshop_id}.png"
    if not tex_to_png(tex_path, atlas_png):
        return None

    try:
        elements = parse_atlas_xml(xml_path.read_text(encoding="utf-8", errors="replace"))
        target = next((e for e in elements if e[0] == mod_info.icon),
                       elements[0] if elements else None)
        if not target:
            return None

        # 先写临时文件再替换：启动预加载和 Mod 页可能同时转换同一个图标，读方不能读到写了一半的 PNG
        staged = cache_dir_path / f".{mod_info.workshop_id}.{os.getpid()}.{threading.get_ident()}.png"
        with Image.open(atlas_png) as img:
            crop_by_uv(img.convert("RGBA"), target[1:]).save(staged)
        try:
            os.replace(staged, cache_path)
        except OSError:
            # 另一方刚替换完、文件正被读取时 Windows 会拒绝替换，用对方的结果即可
            staged.unlink(missing_ok=True)
            if not cache_path.is_file():
                return None
        if source is not None:
            _write_stamp(cache_path, source[0])
    except Exception:
        return None
    finally:
        # 尽力删除中间的整张 atlas PNG；文件可能被杀软临时锁住，失败无害（下次覆盖）
        try:
            atlas_png.unlink(missing_ok=True)
        except OSError:
            pass

    return cache_path
