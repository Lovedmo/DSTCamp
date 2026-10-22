"""DST modoverrides.lua 文件的 mod 配置管理器。"""

from pathlib import Path

from dstools.shared.lua_parser import parse_lua_file, serialize_lua_table
from dstools.models import Cluster, ModEntry, ModOverrides

# 科雷提供的存档级开关（ModIndex:ApplyEnabledOverrides 特判此键）：开启后玩家加入时本地启用的客户端 Mod
# 被临时禁用。游戏只看值的真假，{ enabled = false } 也是真，所以关闭时必须删掉整个键。
# 只对专服生效：本地存档每次由游戏客户端开服都会按启用的 Mod 重写 modoverrides.lua，这个键会被冲掉。
CLIENT_MODS_DISABLED_KEY = "client_mods_disabled"


def _lua_truthy(value) -> bool:
    return value is not None and value is not False


def load_mod_overrides(path: Path) -> ModOverrides:
    """从 modoverrides.lua 加载 Mod 覆盖配置。"""
    mod_overrides = ModOverrides(path=path)

    if not path.exists():
        return mod_overrides

    try:
        raw = parse_lua_file(path)
    except Exception:
        # 解析失败就返回空的覆盖配置
        return mod_overrides

    mod_overrides.client_mods_disabled = _lua_truthy(raw.pop(CLIENT_MODS_DISABLED_KEY, None))
    for workshop_id, mod_data in raw.items():
        if not isinstance(mod_data, dict):
            continue

        entry = ModEntry(
            workshop_id=workshop_id,
            enabled=mod_data.get("enabled", True),
            configuration_options=mod_data.get("configuration_options", {}),
        )
        mod_overrides.mods[workshop_id] = entry

    return mod_overrides


def save_mod_overrides(mod_overrides: ModOverrides) -> None:
    """把 Mod 覆盖配置写回文件。"""
    data = {}
    for workshop_id, entry in mod_overrides.mods.items():
        data[workshop_id] = {
            "configuration_options": entry.configuration_options,
            "enabled": entry.enabled,
        }
    if mod_overrides.client_mods_disabled:
        data[CLIENT_MODS_DISABLED_KEY] = {"enabled": True}

    lua_text = serialize_lua_table(data)
    mod_overrides.path.parent.mkdir(parents=True, exist_ok=True)
    mod_overrides.path.write_text(lua_text, encoding="utf-8")


def enable_mod(mod_overrides: ModOverrides, workshop_id: str) -> None:
    """启用一个 Mod（如 "workshop-378160973"），不存在则添加。"""
    if workshop_id in mod_overrides.mods:
        mod_overrides.mods[workshop_id].enabled = True
    else:
        mod_overrides.mods[workshop_id] = ModEntry(
            workshop_id=workshop_id,
            enabled=True,
            configuration_options={},
        )


def list_mods(mod_overrides: ModOverrides) -> list[ModEntry]:
    """列出覆盖配置中的所有 Mod。"""
    return list(mod_overrides.mods.values())


def sync_mods(source: ModOverrides, target: ModOverrides) -> None:
    """用 source 的 mods 与客户端 Mod 开关整体替换 target 的（原地修改，保留 target 自己的文件路径）。"""
    target.mods.clear()
    target.client_mods_disabled = source.client_mods_disabled
    for workshop_id, entry in source.mods.items():
        target.mods[workshop_id] = ModEntry(
            workshop_id=entry.workshop_id,
            enabled=entry.enabled,
            configuration_options=dict(entry.configuration_options),
        )


def _shard_overrides_path(shard) -> Path:
    return shard.mod_overrides_path or shard.path / "modoverrides.lua"


def client_mods_disabled(cluster: Cluster) -> bool:
    """存档任一世界开启了禁用客户端 Mod（读不了的文件当作未开启）。"""
    return any(load_mod_overrides(_shard_overrides_path(shard)).client_mods_disabled for shard in cluster.shards)


def set_client_mods_disabled(cluster: Cluster, disabled: bool) -> int:
    """在存档所有世界的 modoverrides.lua 里写入或删除禁用客户端 Mod 键，返回改写的文件数。

    整个存档统一设置：玩家换世界时按目标世界的设置重新判断。只增删这一个键，其余内容按原样解析后写回；
    先全部解析成功再动手，解析失败直接抛出，不能当成空配置写回把 Mod 清掉。"""
    tables = {}
    for shard in cluster.shards:
        path = _shard_overrides_path(shard)
        if path.is_file():
            data = parse_lua_file(path)
            tables[path] = data if isinstance(data, dict) else {}
        elif disabled:
            tables[path] = {}
    written = 0
    for path, data in tables.items():
        if disabled:
            if data.get(CLIENT_MODS_DISABLED_KEY) == {"enabled": True}:
                continue
            data[CLIENT_MODS_DISABLED_KEY] = {"enabled": True}
        elif data.pop(CLIENT_MODS_DISABLED_KEY, None) is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize_lua_table(data), encoding="utf-8")
        written += 1
    return written
