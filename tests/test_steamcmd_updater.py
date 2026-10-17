"""SteamCMD 更新：安装目录布局与失败重试。"""

from __future__ import annotations

import gzip
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from _harness import run  # noqa: E402

from dstools.features.local_service import steamcmd_updater  # noqa: E402
from dstools.shared import app_settings  # noqa: E402


def _make_steamcmd_install(root: Path, *, buildid: str = "100") -> None:
    """按真机布局：清单在 force_install_dir/steamapps 下，专服文件直接在 force_install_dir 下。"""
    (root / "bin64").mkdir(parents=True)
    (root / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64.exe").write_bytes(b"")
    (root / "steamapps").mkdir()
    (root / "steamapps" / "appmanifest_343050.acf").write_text(
        '"AppState"\n{\n "appid" "343050"\n "StateFlags" "4"\n'
        ' "installdir" "Don\'t Starve Together Dedicated Server"\n'
        f' "buildid" "{buildid}"\n "BytesToDownload" "0"\n}}\n',
        encoding="utf-8",
    )


def test_steamcmd_install_layout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "dstserver"
        assert not steamcmd_updater.is_steam_library_install(root)
        assert steamcmd_updater.is_steam_library_install(
            Path(tmp) / "steamapps" / "common" / "Don't Starve Together Dedicated Server")

        missing = steamcmd_updater.snapshot_install(root)
        assert missing.manifest_path is None and missing.install_dir is None

        _make_steamcmd_install(root)
        snapshot = steamcmd_updater.snapshot_install(root)
        # 清单里的 installdir 不能拼成 steamapps/common/...，实际目录就是 force_install_dir
        assert snapshot.install_dir == root and snapshot.build_id == "100"
        assert steamcmd_updater.update_succeeded(0, snapshot)
        assert not steamcmd_updater.update_succeeded(7, snapshot)


def test_run_update_retries_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "dstserver"
        codes = iter([7, 0])
        retries = []

        def fake_run(command, on_line, cancel_event):
            code = next(codes)
            if code == 0:
                _make_steamcmd_install(root)  # 第二次才真正装好
            return code

        with patch.object(steamcmd_updater, "_run_once", side_effect=fake_run):
            snapshot = steamcmd_updater.run_update(
                Path("steamcmd.exe"), root, on_line=lambda _l: None, on_retry=retries.append)
        assert retries == [7] and snapshot.build_id == "100"

        codes = iter([7, 7])
        with patch.object(steamcmd_updater, "_run_once", side_effect=lambda *a: next(codes)):
            try:
                steamcmd_updater.run_update(Path("steamcmd.exe"), Path(tmp) / "none", on_line=lambda _l: None)
            except RuntimeError:
                pass
            else:
                raise AssertionError("两次都失败时应抛 RuntimeError")


def test_suspended_update_is_retried() -> None:
    """实测：全新 SteamCMD 首次校验被挂起，空等后仍打印 Success! 且退出码 0，必须按失败重试。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "dstserver"
        _make_steamcmd_install(root)  # 已装好的专服：清单本身看不出这次什么也没做
        steamcmd = Path(tmp) / "steamcmd" / "steamcmd.exe"
        log = steamcmd.parent / "logs" / "content_log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("", encoding="utf-8")
        attempts, events = [], []

        def fake_run(command, on_line, cancel_event):
            attempts.append(command)
            if len(attempts) == 1:
                with log.open("a", encoding="utf-8") as file:
                    file.write("[2026-10-17 23:44:10] AppID 343050 scheduler finished : "
                               "removed from schedule (result Suspended, state 0xc) \n")
                time.sleep(1.5)  # 等日志跟踪读到
            return 0

        with patch.object(steamcmd_updater, "_run_once", side_effect=fake_run):
            steamcmd_updater.run_update(steamcmd, root, on_line=lambda _l: None,
                                        on_retry=lambda c: events.append(("retry", c)),
                                        on_suspended=lambda: events.append("suspended"))
        assert len(attempts) == 2 and events == ["suspended"]


def test_steamcmd_tasks_run_one_at_a_time() -> None:
    """同时跑两个 SteamCMD 会共写日志（实测），后来的任务须排队，等待中可取消。"""
    import threading

    release = threading.Event()
    running = threading.Event()
    order = []

    def fake_run(command, on_line, cancel_event, **_kwargs):
        order.append(command[2])
        if command[2] == "first":
            running.set()
            release.wait(5)
        return 0

    with patch.object(steamcmd_updater, "_run_once", side_effect=fake_run):
        first = threading.Thread(target=steamcmd_updater.login, args=(Path("steamcmd.exe"), "first", "pw"),
                                 kwargs={"on_line": lambda _l: None})
        first.start()
        assert running.wait(5)

        cancel, lines = threading.Event(), []
        cancel.set()
        try:
            steamcmd_updater.login(Path("steamcmd.exe"), "cancelled", "pw", on_line=lines.append, cancel_event=cancel)
        except InterruptedError:
            pass
        else:
            raise AssertionError("排队等待中取消应抛 InterruptedError")
        assert len(lines) == 1 and "cancelled" not in order

        second = threading.Thread(target=steamcmd_updater.login, args=(Path("steamcmd.exe"), "second", "pw"),
                                  kwargs={"on_line": lines.append})
        second.start()
        time.sleep(0.3)
        assert order == ["first"]  # 第一个没结束，第二个还在排队
        release.set()
        first.join(5)
        second.join(5)
        assert order == ["first", "second"] and len(lines) == 2


def test_account_login_failure_not_retried() -> None:
    command = steamcmd_updater.build_command(Path("steamcmd.exe"), Path("C:/dst"), account="hostbot")
    assert command[command.index("+login") + 1] == "hostbot"  # 只传用户名，密码靠 SteamCMD 缓存
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def fake_run(command, on_line, cancel_event):
            calls.append(command)
            return steamcmd_updater.LOGIN_FAILED_EXIT_CODE

        with patch.object(steamcmd_updater, "_run_once", side_effect=fake_run):
            try:
                steamcmd_updater.run_update(Path("steamcmd.exe"), Path(tmp), on_line=lambda _l: None,
                                            account="hostbot")
            except steamcmd_updater.SteamcmdLoginError:
                pass
            else:
                raise AssertionError("登录失败应抛 SteamcmdLoginError")
        # 连续登录失败会触发 Steam 限制，只试一次
        assert len(calls) == 1

    # 真机输出：缺验证码时先提示 Steam Guard code，读不到就 Account Logon Denied
    assert steamcmd_updater.needs_guard_code(["Steam Guard code:ERROR (Account Logon Denied)"])
    assert not steamcmd_updater.needs_guard_code(["Logging in user 'x' [U:1:0] to Steam Public...ERROR (Invalid Password)"])


def test_backend_update_blocker() -> None:
    from dstools.features.local_service.server_runtime import RuntimeKind, ServerRuntime
    from dstools.features.local_service.steam_backend import SteamBackend, update_blocker

    library = ServerRuntime(RuntimeKind.DEDICATED,
                            Path("D:/Steam/steamapps/common/Don't Starve Together Dedicated Server"))
    custom = ServerRuntime(RuntimeKind.DEDICATED, Path("C:/dstserver"))
    client = ServerRuntime(RuntimeKind.CLIENT, Path("D:/Steam/steamapps/common/Don't Starve Together"))

    # Steam 客户端方式：管不了 SteamCMD 装的目录，其余照旧
    assert update_blocker(SteamBackend.CLIENT, library) is None
    assert update_blocker(SteamBackend.CLIENT, client) is None
    assert update_blocker(SteamBackend.CLIENT, custom) == "local.backend_client_steamcmd_dir"

    # SteamCMD 方式：只更新 SteamCMD 装的独立专服
    with patch.object(steamcmd_updater, "find_steamcmd", return_value=None):
        app_settings.set_steamcmd_path(Path("C:/gone/steamcmd.exe"))
        assert update_blocker(SteamBackend.STEAMCMD, custom) == "local.backend_steamcmd_missing"
        app_settings.set_steamcmd_path(None)
        with patch.object(steamcmd_updater, "bundled_steamcmd_path", return_value=Path("C:/缓存/steamcmd.exe")):
            assert update_blocker(SteamBackend.STEAMCMD, custom) == "local.backend_steamcmd_cache_non_ascii"
    with patch.object(steamcmd_updater, "find_steamcmd", return_value=Path("C:/steamcmd/steamcmd.exe")):
        assert update_blocker(SteamBackend.STEAMCMD, custom) is None
        assert update_blocker(SteamBackend.STEAMCMD, library) == "local.backend_steamcmd_library_dir"
        assert update_blocker(SteamBackend.STEAMCMD, client) == "local.backend_steamcmd_client_runtime"
        assert update_blocker(SteamBackend.STEAMCMD, None) is None  # 没有开服程序时可全新安装


def test_bundled_steamcmd_released_once() -> None:
    target = steamcmd_updater.ensure_bundled_steamcmd()
    official = gzip.decompress((PROJECT_ROOT / "tools" / "steamcmd" / "steamcmd.exe.gz").read_bytes())
    # 原样释放（许可协议不允许修改程序），并附带许可协议
    assert target == steamcmd_updater.bundled_steamcmd_path() and target.read_bytes() == official
    assert (target.parent / "LICENSE.txt").is_file()
    assert steamcmd_updater.find_steamcmd() == target  # 没改选时用内置的

    # SteamCMD 自更新会替换这个文件，之后不能被随包的旧版本覆盖回去
    target.write_bytes(b"self-updated")
    assert steamcmd_updater.ensure_bundled_steamcmd().read_bytes() == b"self-updated"


def test_download_workshop_items_reads_acf() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        steamcmd = Path(tmp) / "steamcmd.exe"
        workshop = Path(tmp) / "steamapps" / "workshop"

        def fake_run(command, on_line, cancel_event, **_kwargs):
            # 按真机结果造现场：V2 解压出 modinfo.lua，V1 只有 *_legacy.bin，不存在的项目什么都没有
            (workshop / "content" / "322330" / "200").mkdir(parents=True)
            (workshop / "content" / "322330" / "200" / "modinfo.lua").write_text("", encoding="utf-8")
            (workshop / "content" / "322330" / "300").mkdir()
            (workshop / "content" / "322330" / "300" / "1_legacy.bin").write_bytes(b"")
            (workshop / "appworkshop_322330.acf").write_text(
                '"AppWorkshop"\n{\n "appid" "322330"\n "WorkshopItemsInstalled"\n {\n'
                '  "200" { "size" "1" "timeupdated" "1651381537" }\n'
                '  "300" { "size" "1" "timeupdated" "1575888768" }\n }\n'
                ' "WorkshopItemDetails"\n {\n }\n}\n', encoding="utf-8")
            return 0

        with patch.object(steamcmd_updater, "_run_once", side_effect=fake_run):
            result = steamcmd_updater.download_workshop_items(steamcmd, ["200", "300", "1"], on_line=lambda _l: None)
        assert result == {"200": 1651381537, "300": 1575888768, "1": 0}

        with patch.object(steamcmd_updater, "_run_once", return_value=steamcmd_updater.LOGIN_FAILED_EXIT_CODE):
            try:
                steamcmd_updater.download_workshop_items(steamcmd, ["200"], on_line=lambda _l: None, account="hostbot")
            except steamcmd_updater.SteamcmdLoginError:
                pass
            else:
                raise AssertionError("登录失效应抛 SteamcmdLoginError")


def test_input_prompt_watch() -> None:
    import threading
    import time

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "console_log.txt"
        log.write_text("[t] old password: \r\n", encoding="utf-8")  # 启动前的旧内容不算
        abort, stop = threading.Event(), threading.Event()
        watcher = threading.Thread(target=steamcmd_updater._watch_input_prompts, args=(log, 1, abort, stop))
        watcher.start()
        time.sleep(0.7)
        with log.open("a", encoding="utf-8") as file:
            # 真机 console_log 格式：给了密码时第一个提示不中止
            file.write("[t] Cached credentials not found.\r\n[t] \npassword: \r\n")
        time.sleep(1.2)
        assert not abort.is_set()
        with log.open("a", encoding="utf-8") as file:
            file.write("[t] Steam Guard code:")  # 第二个提示没有可给的输入
        watcher.join(3)
        stop.set()
        assert abort.is_set()


def test_steamcmd_mod_classify() -> None:
    from dstools.features.mod.steamcmd_mod_status import SteamcmdModState, classify
    from dstools.features.mod.workshop_webapi import RemoteWorkshopItem

    remote = {"1": RemoteWorkshopItem(200, "A"), "2": RemoteWorkshopItem(200), "3": RemoteWorkshopItem(200)}
    states = classify(["1", "2", "3", "4"], {"1": 100, "2": 200}, remote)
    assert [states[w].state for w in ("1", "2", "3", "4")] == [
        SteamcmdModState.OUTDATED, SteamcmdModState.CURRENT, SteamcmdModState.MISSING, SteamcmdModState.UNAVAILABLE]
    assert [w for w, s in states.items() if s.needs_download] == ["1", "3"]


def test_workshop_dirs_follow_channel() -> None:
    from dstools.features.local_service.steam_backend import SteamBackend, set_backend
    from dstools.features.mod import parser

    with tempfile.TemporaryDirectory() as tmp:
        steam_library = Path(tmp) / "Steam"
        (steam_library / "steamapps" / "workshop" / "content" / "322330").mkdir(parents=True)
        steamcmd_dir = Path(tmp) / "steamcmd"
        steamcmd_dir.mkdir()
        app_settings.set_steamcmd_path(steamcmd_dir / "steamcmd.exe")
        with patch.object(parser, "find_all_steam_libraries", return_value=[steam_library]):
            set_backend(SteamBackend.CLIENT)
            assert parser.find_shared_ugc_directory() == steam_library / "steamapps" / "workshop"

            # SteamCMD 通道不再看 Steam 库；还没下载过 Mod 时也给出 -ugc_directory，缺失检查才能提示下载
            set_backend(SteamBackend.STEAMCMD)
            workshop = steamcmd_dir / "steamapps" / "workshop"
            assert parser.find_shared_ugc_directory() == workshop
            assert parser.find_workshop_dir() is None
            (workshop / "content" / "322330").mkdir(parents=True)
            assert parser.find_workshop_dir() == workshop / "content" / "322330"
        set_backend(SteamBackend.CLIENT)
        app_settings.set_steamcmd_path(None)


if __name__ == "__main__":
    run(globals())
