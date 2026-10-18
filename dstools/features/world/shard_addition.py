"""给已有专服存档新增一个世界分片：只新建分片目录、按需打开多世界设置，不碰已有世界的存档。

新分片在下次开服时由游戏生成；Mod 配置复制主世界的 modoverrides.lua，各世界保持一致。
"""

import copy
import os
from pathlib import Path
import secrets
import shutil

from dstools.features.cluster_config.config_manager import CLUSTER_INI_DEFAULTS
from dstools.features.mod.sync import get_enabled_mod_ids
from dstools.features.world.creation import shard_level_data, WorldShardPlan
from dstools.features.world.defaults import (
    default_plan_for_location,
    default_plans_from_cluster,
    find_verified_template,
    shard_plan_from_template,
)
from dstools.features.world.location_profiles import (
    CAVE_LOCATION,
    FOREST_LOCATION,
    get_location_definition,
    location_requirements_met,
    resolve_world_location_profile,
    with_required_dependencies,
)
from dstools.models import Cluster, Platform, SaveSource, ShardConfig
from dstools.shared.ini_parser import parse_cluster_ini, parse_server_ini, write_cluster_ini, write_server_ini
from dstools.shared.lua_parser import serialize_lua_table
from dstools.shared.server_ports import (
    DEFAULT_MASTER_PORT,
    DEFAULT_STEAM_AUTH_PORT,
    DEFAULT_STEAM_MASTER_PORT,
    LAN_SERVER_PORT_FALLBACK,
    LAN_SERVER_PORT_MAX,
    LAN_SERVER_PORT_MIN,
    collect_cluster_port_claims,
    next_free_port,
    scan_udp_ports,
)

# 与创建向导的额外世界命名一致
_SHARD_NAME_BASES = {"forest": "Forest", "cave": "Caves", "porkland": "Porkland",
                     "shipwrecked": "Shipwrecked", "volcanoworld": "Volcano"}

# 打开多世界时 cluster.ini [SHARD] 必须有的字段，缺失时补官方默认值（master_port 另行避开已占用端口）
_SHARD_REQUIRED_KEYS = ("bind_ip", "master_ip", "cluster_key")


def _master_shard(cluster: Cluster):
    """主世界分片：server.ini [SHARD] is_master 为真的那个，没写 is_master 时取 Master 目录。"""
    for shard in cluster.shards:
        if (shard.path / "server.ini").is_file() and parse_server_ini(shard.path / "server.ini").shard.get("is_master"):
            return shard
    return next((shard for shard in cluster.shards if shard.name == "Master"), None)


def available_locations(cluster: Cluster) -> tuple[str, ...]:
    """按存档已启用的 Mod 列出可以新增的世界类型（与创建向导"添加新的世界"一致）。"""
    profile = resolve_world_location_profile(get_enabled_mod_ids(cluster))
    return tuple(dict.fromkeys(profile.master_locations + profile.caves_locations))


def next_shard_name(cluster: Cluster, location: str) -> str:
    """按世界类型取不重复的目录名：Forest、Forest_2……（不区分大小写）。"""
    base = _SHARD_NAME_BASES.get(location, "World")
    occupied = {path.name.casefold() for path in cluster.path.iterdir()}
    if base.casefold() not in occupied:
        return base
    index = 2
    while f"{base}_{index}".casefold() in occupied:
        index += 1
    return f"{base}_{index}"


def initial_plan(cluster: Cluster, location: str, klei_root: Path | None) -> WorldShardPlan:
    """新世界的初始关卡数据：优先复制本存档同类世界（完整定义，设置也与旧世界一致），
    其次取游戏生成的其他存档模板，Mod 世界用已核对的默认值。"""
    for shard in cluster.shards:
        if shard.leveldata_path is None or not shard.leveldata_path.is_file():
            continue
        try:
            plan = shard_plan_from_template(shard.leveldata_path)
        except ValueError:
            continue
        if plan.location == location:
            return plan
    if klei_root is not None and location in (FOREST_LOCATION, CAVE_LOCATION):
        try:
            master, caves = default_plans_from_cluster(find_verified_template(klei_root, FOREST_LOCATION))
            return master if location == FOREST_LOCATION else caves
        except (FileNotFoundError, ValueError):
            pass
    return default_plan_for_location(location)


def used_ports_elsewhere(clusters, cluster: Cluster) -> set[int]:
    """其他 Steam 专服存档配置的端口加上系统当前占用的 UDP 端口（netstat，较慢，放后台调用）。"""
    used: set[int] = set()
    for other in clusters:
        if other.path == cluster.path or other.source != SaveSource.SERVER or other.platform != Platform.STEAM:
            continue
        claims, _issues = collect_cluster_port_claims(other)
        used.update(claim.port for claim in claims)
    scan = scan_udp_ports()
    if scan.ok:
        used.update(port for ports in scan.ports_by_pid.values() for port in ports)
    return used


def next_shard_id(cluster: Cluster) -> int:
    """新分片的 [SHARD] id：取现有编号最大值加一（主世界固定为 1）。"""
    used = {1}
    for shard in cluster.shards:
        path = shard.path / "server.ini"
        if not path.is_file():
            continue
        try:
            used.add(int(str(parse_server_ini(path).shard.get("id", "")).strip()))
        except ValueError:
            continue
    return max(used) + 1


def validate_new_shard(cluster: Cluster, shard_name: str, plan: WorldShardPlan) -> None:
    """检查能否给该存档加这个世界，不能时抛出带中文原因的 ValueError。"""
    if cluster.source != SaveSource.SERVER:
        raise ValueError("只有专服存档可以新增世界，本地存档请在游戏内创建")
    if not (cluster.path / "cluster.ini").is_file():
        raise ValueError("存档里没有 cluster.ini")
    if _master_shard(cluster) is None:
        raise ValueError("存档里找不到主世界")
    if not shard_name or shard_name.startswith(".") or any(ch in shard_name for ch in '\\/:*?"<>|'):
        raise ValueError(f"非法世界名称: {shard_name}")
    # Windows 目录名不区分大小写；server.ini 里的分片名同样要唯一
    if any(path.name.casefold() == shard_name.casefold() for path in cluster.path.iterdir()):
        raise ValueError(f"存档里已有同名目录: {shard_name}")
    for shard in cluster.shards:
        path = shard.path / "server.ini"
        if path.is_file() and str(parse_server_ini(path).shard.get("name", "")).casefold() == shard_name.casefold():
            raise ValueError(f"已有世界使用这个分片名: {shard_name}")
    get_location_definition(plan.location)
    effective = with_required_dependencies(get_enabled_mod_ids(cluster))
    if not location_requirements_met(plan.location, effective):
        raise ValueError(f"{plan.location} 世界缺少所需 Mod，请先在 Mod 页启用")
    if not plan.preset_id:
        raise ValueError("世界预设不能为空")
    # 游戏把 leveldataoverride.lua 当完整关卡定义整体替换，缺 task_set 世界生成不出来
    if "task_set" not in shard_level_data(plan)["overrides"]:
        raise ValueError("缺少完整的世界生成数据（task_set），请先用游戏创建一个带洞穴的世界作为模板")


def plan_shard_config(cluster: Cluster, shard_name: str, used_ports) -> ShardConfig:
    """新分片的 server.ini：编号接在已有分片之后，三个端口避开 used_ports 和本存档已占用的端口。"""
    claims, _issues = collect_cluster_port_claims(cluster)
    occupied = set(used_ports) | {claim.port for claim in claims}
    network = parse_cluster_ini(cluster.path / "cluster.ini").network
    if network.get("lan_only_cluster") or network.get("offline_cluster"):
        # 离线/仅局域网时游戏端口必须在 10998..11018，否则游戏会退回 10999
        candidates = [LAN_SERVER_PORT_FALLBACK, LAN_SERVER_PORT_MIN,
                      *range(LAN_SERVER_PORT_MIN + 2, LAN_SERVER_PORT_MAX + 1)]
        server_port = next((port for port in candidates if port not in occupied), None)
        if server_port is None:
            raise ValueError(f"{LAN_SERVER_PORT_MIN}..{LAN_SERVER_PORT_MAX} 中没有可用端口")
    else:
        server_port = next_free_port(LAN_SERVER_PORT_MIN, occupied)
    occupied.add(server_port)
    master_server_port = next_free_port(DEFAULT_STEAM_MASTER_PORT, occupied)
    occupied.add(master_server_port)
    authentication_port = next_free_port(DEFAULT_STEAM_AUTH_PORT, occupied)
    return ShardConfig(
        network={"server_port": server_port},
        shard={"is_master": False, "name": shard_name, "id": next_shard_id(cluster)},
        account={"encode_user_path": True},
        steam={"master_server_port": master_server_port, "authentication_port": authentication_port},
    )


def add_shard(cluster: Cluster, shard_name: str, plan: WorldShardPlan, used_ports=()) -> Path:
    """在已停止的存档里新建一个世界分片目录，返回其路径（调用方负责确认存档未运行）。

    ``used_ports`` 为其他存档和系统已占用的端口；本存档自己的端口会自动避开。
    """
    validate_new_shard(cluster, shard_name, plan)
    config = plan_shard_config(cluster, shard_name, used_ports)
    master = _master_shard(cluster)

    cluster_ini_path = cluster.path / "cluster.ini"
    cluster_config = parse_cluster_ini(cluster_ini_path)
    new_cluster_config = copy.deepcopy(cluster_config)
    shard_section = new_cluster_config.shard
    shard_section["shard_enabled"] = True
    for key in _SHARD_REQUIRED_KEYS:
        if shard_section.get(key) in (None, ""):
            shard_section[key] = CLUSTER_INI_DEFAULTS[("SHARD", key)]
    if shard_section.get("master_port") in (None, ""):
        claims, _issues = collect_cluster_port_claims(cluster)
        occupied = set(used_ports) | {claim.port for claim in claims if claim.field != "master_port"}
        occupied.update((config.network["server_port"], config.steam["master_server_port"],
                         config.steam["authentication_port"]))
        shard_section["master_port"] = next_free_port(DEFAULT_MASTER_PORT, occupied)
    master_ini_path = master.path / "server.ini"
    master_config = parse_server_ini(master_ini_path)
    master_needs_flag = not master_config.shard.get("is_master")
    if master_needs_flag:
        master_config.shard["is_master"] = True

    # 先在存档目录里的临时目录写好，server.ini 最后写（没有 server.ini 的目录不会被识别成世界），再整体改名
    while True:
        temp_dir = cluster.path / f".{shard_name}.{secrets.token_hex(8)}"
        try:
            temp_dir.mkdir()
            break
        except FileExistsError:
            continue
    destination = cluster.path / shard_name
    original_cluster_ini = cluster_ini_path.read_bytes()
    original_master_ini = master_ini_path.read_bytes()
    try:
        (temp_dir / "leveldataoverride.lua").write_text(
            serialize_lua_table(shard_level_data(plan)) + "\n", encoding="utf-8")
        mod_source = master.path / "modoverrides.lua"
        if mod_source.is_file():
            shutil.copyfile(mod_source, temp_dir / "modoverrides.lua")
        write_server_ini(config, temp_dir / "server.ini")
        if new_cluster_config != cluster_config:
            write_cluster_ini(new_cluster_config, cluster_ini_path)
        if master_needs_flag:
            write_server_ini(master_config, master_ini_path)
        os.replace(temp_dir, destination)
        return destination
    except Exception:
        cluster_ini_path.write_bytes(original_cluster_ini)
        master_ini_path.write_bytes(original_master_ini)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
