"""世界预设命令行：在任意目录用存档快速核对、保存、套用游戏的自定义世界预设（与世界设置页同一套逻辑）。

    python -X utf8 -m dstools.features.world.preset_cli check --cluster Cluster_1
    python -X utf8 -m dstools.features.world.preset_cli list
    python -X utf8 -m dstools.features.world.preset_cli save --cluster Cluster_1 --shard Master --name 测试用
    python -X utf8 -m dstools.features.world.preset_cli apply --cluster Cluster_1 --shard Master --preset 测试用

check、list 只读；save 写游戏的 world_presets，apply 写存档（先备份）。加 --json 输出机器可读结果。
账号沿用 DSTCamp 当前选中的账号（与界面一致），--user-dir 可直接指定账号目录。
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from dstools.features.mod.shardindex import ShardIndexError
from dstools.features.world import page_data, user_presets
from dstools.models import Platform, SaveSource


class CliError(Exception):
    """参数或存档状态不允许继续，返回码 2。"""


def _find_cluster(env, name: str, source: str | None):
    """按目录名或完整路径找存档；服务器与本地存档同名时要求 --source 区分。"""
    path = Path(name)
    matches = [c for c in env.clusters
               if (str(c.path).lower() == str(path).lower() if path.is_absolute() else c.name.lower() == name.lower())
               and (source is None or c.source.value == source)]
    if not matches:
        raise CliError(f"找不到存档：{name}")
    if len(matches) > 1:
        options = "\n".join(f"  {c.source.value}  {c.path}" for c in matches)
        raise CliError(f"有多个同名存档，请用 --source 或完整路径区分：\n{options}")
    return matches[0]


def _shard_names(cluster, shards: list[str] | None) -> list[str]:
    names = [s.name for s in cluster.shards]
    if not shards:
        return names
    missing = [s for s in shards if s not in names]
    if missing:
        raise CliError(f"存档 {cluster.name} 没有世界：{'、'.join(missing)}（现有：{'、'.join(names)}）")
    return shards


def _user_dir(env, args, platform: Platform) -> Path:
    if args.user_dir:
        return Path(args.user_dir)
    account = env.current_account(platform)
    if account is None or not account.path.is_dir():
        raise CliError("没有找到饥荒账号目录，请先启动一次游戏，或用 --user-dir 指定")
    return account.path


def _load_ok(cluster, shard: str) -> page_data.WorldPageData:
    data = page_data.load_world_page(cluster, shard)
    if data.status != page_data.STATUS_OK:
        raise CliError(f"{shard} 的世界设置读取失败：{data.status} {data.error}".rstrip())
    return data


def _preset_parts(data, user_dir: Path | None) -> dict:
    table = page_data.current_override_table(data)
    settings, worldgen, unknown = user_presets.split_overrides(table, data.location, data.mod_settings)
    settings_base, worldgen_base = user_presets.resolve_base_ids(data.preset.raw, data.location, user_dir)
    return {"settings": settings, "worldgen": worldgen, "unknown": unknown,
            "settings_base": settings_base, "worldgen_base": worldgen_base}


def cmd_check(env, args) -> dict:
    """每个世界：拆分 → 存到临时目录 → 读回比对 → 把自己的预设套回自己（应无改动）。"""
    cluster = _find_cluster(env, args.cluster, args.source)
    try:
        user_dir = _user_dir(env, args, cluster.platform)
    except CliError:
        user_dir = None  # 只影响"由自定义预设生成的世界"找基础预设，找不到时按默认预设核对
    shards = []
    with tempfile.TemporaryDirectory(prefix="dstcamp_preset_check_") as tmp:
        for shard in _shard_names(cluster, args.shards):
            data = _load_ok(cluster, shard)
            parts = _preset_parts(data, user_dir)
            saved = user_presets.save_preset(Path(tmp), f"check {shard}", "", parts["settings_base"],
                                             parts["worldgen_base"], parts["settings"], parts["worldgen"])
            reread = next((p for p in user_presets.list_presets(Path(tmp))[0] if p.preset_id == saved.preset_id), None)
            roundtrip = (reread is not None and reread.settings == parts["settings"]
                         and reread.worldgen == parts["worldgen"] and reread.location == data.location)
            changed, skipped = page_data.apply_preset_values(data, saved.overrides)
            shards.append({
                "shard": shard, "location": data.location,
                "settings_base": parts["settings_base"], "worldgen_base": parts["worldgen_base"],
                "settings_count": len(parts["settings"]), "worldgen_count": len(parts["worldgen"]),
                "unknown_keys": parts["unknown"], "roundtrip_ok": roundtrip,
                "self_apply_changed": changed, "self_apply_skipped": skipped,
                "unverified_mods": [list(m) for m in data.unverified_mods],
            })
    ok = all(s["roundtrip_ok"] and not s["self_apply_changed"] for s in shards)
    return {"command": "check", "cluster": str(cluster.path), "ok": ok, "shards": shards}


def cmd_list(env, args) -> dict:
    platform = Platform.WEGAME if args.platform == "wegame" else Platform.STEAM
    user_dir = _user_dir(env, args, platform)
    presets, errors = user_presets.list_presets(user_dir)
    if args.location:
        presets = [p for p in presets if p.location == args.location]
    return {"command": "list", "user_dir": str(user_dir), "ok": not errors, "errors": errors, "presets": [
        {"id": p.preset_id, "name": p.name, "description": p.description, "location": p.location,
         "base": p.base_id, "settings_count": len(p.settings), "worldgen_count": len(p.worldgen)}
        for p in presets]}


def cmd_save(env, args) -> dict:
    cluster = _find_cluster(env, args.cluster, args.source)
    shard = _shard_names(cluster, [args.shard])[0]
    user_dir = _user_dir(env, args, cluster.platform)
    name = user_presets.validate_name(args.name)
    preset_id = user_presets.preset_id_for_name(name)
    exists = any(p.preset_id.upper() == preset_id.upper() for p in user_presets.list_presets(user_dir)[0])
    if exists and not args.overwrite:
        raise CliError(f"已有名为「{name}」的预设，确认覆盖请加 --overwrite")
    data = _load_ok(cluster, shard)
    parts = _preset_parts(data, user_dir)
    saved = user_presets.save_preset(user_dir, name, args.desc, parts["settings_base"], parts["worldgen_base"],
                                     parts["settings"], parts["worldgen"])
    return {"command": "save", "ok": True, "id": saved.preset_id, "path": str(user_presets.presets_dir(user_dir)),
            "location": data.location, "overwritten": exists, "settings_count": len(saved.settings),
            "worldgen_count": len(saved.worldgen), "unknown_keys": parts["unknown"],
            "note": "游戏只在启动时读取预设，游戏开着时需重启游戏才能看到"}


def _check_writable(cluster) -> str:
    """本地存档：可能在运行时拒绝；游戏开着时返回提醒（游戏会用缓存覆盖，需回到开始页面再开存档）。"""
    if cluster.source == SaveSource.SERVER:
        return ""
    from dstools.features.mod.shardindex import LOCAL_SAVE_OPEN, LOCAL_SAVE_RUNNING, local_save_play_state
    state = local_save_play_state(cluster)
    if state == LOCAL_SAVE_RUNNING:
        raise CliError("这个本地存档可能正在运行，开服进程会覆盖修改，请先关闭存档")
    return "游戏客户端开着：回到开始页面（或重启游戏）后再开这个存档，修改才会生效" if state == LOCAL_SAVE_OPEN else ""


def cmd_apply(env, args) -> dict:
    cluster = _find_cluster(env, args.cluster, args.source)
    shard = _shard_names(cluster, [args.shard])[0]
    user_dir = _user_dir(env, args, cluster.platform)
    wanted = args.preset.strip()
    # 可以给名称（按游戏规则转成 ID）或完整 ID
    as_id = user_presets.preset_id_for_name(wanted)
    ids = {as_id, as_id[len(user_presets.PRESET_PREFIX):]}
    presets = user_presets.list_presets(user_dir)[0]
    preset = next((p for p in presets if p.preset_id in ids), None)
    if preset is None:
        raise CliError(f"找不到预设：{args.preset}（现有：{'、'.join(p.name for p in presets) or '无'}）")
    data = _load_ok(cluster, shard)
    if preset.location != data.location:
        raise CliError(f"预设是 {preset.location or '未知'} 世界的，{shard} 是 {data.location}")
    if not data.can_save:
        raise CliError(data.local_index_error or "这个世界当前无法保存")
    changed, skipped = page_data.apply_preset_values(data, preset.overrides)
    result = {"command": "apply", "ok": True, "preset": preset.preset_id, "shard": shard, "changed": changed,
              "skipped": skipped, "dry_run": args.dry_run, "backup": "", "note": ""}
    if args.dry_run or not changed:
        return result
    note = _check_writable(cluster)
    from dstools.features.local_service.backup_manager import create_backup
    result["backup"] = str(create_backup(cluster.path))
    page_data.save_world_page(data)
    if any(key in preset.worldgen for key in changed) and page_data.generation_requires_reset(data):
        note = (note + "；" if note else "") + "改了世界生成项，这个世界已生成过，需要重置世界才会按新设置生成"
    result["note"] = note
    return result


def _print_text(result: dict) -> None:
    command = result["command"]
    if command == "check":
        print(f"存档：{result['cluster']}")
        for s in result["shards"]:
            print(f"\n[{s['shard']}] {s['location']}  基础预设 {s['settings_base']}/{s['worldgen_base']}")
            print(f"  世界规则 {s['settings_count']} 项，世界生成 {s['worldgen_count']} 项")
            print(f"  读回一致：{'是' if s['roundtrip_ok'] else '否'}；套回自身改动：{len(s['self_apply_changed'])} 项"
                  + (f" {s['self_apply_changed']}" if s['self_apply_changed'] else ""))
            if s["unknown_keys"]:
                print(f"  未登记、不写入预设（{len(s['unknown_keys'])}）：{', '.join(s['unknown_keys'])}")
            if s["self_apply_skipped"]:
                print(f"  套用时跳过（{len(s['self_apply_skipped'])}）：{', '.join(s['self_apply_skipped'])}")
            for name, installed, verified in s["unverified_mods"]:
                print(f"  Mod 版本未核对：{name} 本机 {installed}，已核对 {verified}")
    elif command == "list":
        print(f"账号目录：{result['user_dir']}")
        for p in result["presets"]:
            print(f"  {p['name']}  [{p['location'] or '未知'}] 基于 {p['base']}  规则 {p['settings_count']} / "
                  f"生成 {p['worldgen_count']}" + (f"  — {p['description']}" if p["description"] else ""))
        if not result["presets"]:
            print("  （没有自定义预设）")
        for error in result["errors"]:
            print(f"  读取失败：{error}")
    elif command == "save":
        print(f"{'已覆盖' if result['overwritten'] else '已保存'} {result['id']}（{result['location']}）→ {result['path']}")
        print(f"  世界规则 {result['settings_count']} 项，世界生成 {result['worldgen_count']} 项；{result['note']}")
        if result["unknown_keys"]:
            print(f"  未登记、没写入：{', '.join(result['unknown_keys'])}")
    elif command == "apply":
        head = "预览（未写入）" if result["dry_run"] else ("已保存" if result["changed"] else "无需改动")
        print(f"{head}：{result['preset']} → {result['shard']}，改动 {len(result['changed'])} 项")
        if result["changed"]:
            print(f"  {', '.join(result['changed'])}")
        if result["skipped"]:
            print(f"  跳过（{len(result['skipped'])}）：{', '.join(result['skipped'])}")
        if result["backup"]:
            print(f"  备份：{result['backup']}")
        if result["note"]:
            print(f"  注意：{result['note']}")
    print("\n结果：" + ("通过" if result["ok"] else "有问题"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="preset_cli", description="核对、保存、套用游戏的自定义世界预设")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON")
    common.add_argument("--user-dir", help="账号目录（默认 DSTCamp 当前账号）")
    cluster_args = argparse.ArgumentParser(add_help=False)
    cluster_args.add_argument("--cluster", required=True, help="存档目录名或完整路径")
    cluster_args.add_argument("--source", choices=[s.value for s in SaveSource], help="服务器/本地存档同名时区分")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[common, cluster_args], help="只读核对：拆分、读回、套回自身")
    check.add_argument("--shards", nargs="+", help="只核对这些世界（默认全部）")
    check.set_defaults(func=cmd_check)

    listing = sub.add_parser("list", parents=[common], help="列出自定义预设")
    listing.add_argument("--platform", choices=["steam", "wegame"], default="steam")
    listing.add_argument("--location", help="只列这个世界类型，如 forest、cave")
    listing.set_defaults(func=cmd_list)

    save = sub.add_parser("save", parents=[common, cluster_args], help="把一个世界存成游戏预设")
    save.add_argument("--shard", required=True)
    save.add_argument("--name", required=True)
    save.add_argument("--desc", default="")
    save.add_argument("--overwrite", action="store_true", help="同名时覆盖")
    save.set_defaults(func=cmd_save)

    apply = sub.add_parser("apply", parents=[common, cluster_args], help="把预设套用到一个世界并保存（先备份）")
    apply.add_argument("--shard", required=True)
    apply.add_argument("--preset", required=True, help="预设名称或 ID")
    apply.add_argument("--dry-run", action="store_true", help="只显示会改什么，不写入")
    apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    from dstools.shared.discovery import discover_environment
    try:
        result = args.func(discover_environment(), args)
    except (CliError, ValueError, OSError, ShardIndexError, user_presets.UserPresetError) as exc:
        if args.json:
            print(json.dumps({"command": args.command, "ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
