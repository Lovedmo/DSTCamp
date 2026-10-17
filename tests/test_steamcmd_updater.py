"""SteamCMD 更新：安装目录布局与失败重试。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.local_service import steamcmd_updater  # noqa: E402


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


if __name__ == "__main__":
    run(globals())
