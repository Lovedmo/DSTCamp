"""SteamCMD 更新：安装目录布局与失败重试。"""

from __future__ import annotations

import gzip
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from _harness import run  # noqa: E402

# 设置与缓存放进临时目录，绝不读写真实 %APPDATA%/DSTCamp。
_SETTINGS_TMP = tempfile.TemporaryDirectory()
os.environ["APPDATA"] = _SETTINGS_TMP.name

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
        assert update_blocker(SteamBackend.STEAMCMD, None) == "local.backend_steamcmd_no_runtime"


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


if __name__ == "__main__":
    run(globals())
