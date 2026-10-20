"""自动更新元数据、下载和完整性校验测试。"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from dstools import __version__
from dstools.shared import auto_update, update_check
from dstools.shared.update_check import UpdatePatch, UpdateRelease
from _harness import run  # noqa: E402


class _Response(io.BytesIO):
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_release_manifest_enables_auto_update() -> None:
    manifest = {
        "version": "1.4.0",
        "files": {
            "DSTCamp-1.4.0.exe": {"size": 4, "sha256": "a" * 64},
        },
        "patches": {
            __version__: {"name": "p.hdiff", "size": 2, "sha256": "b" * 64, "from_sha256": "c" * 64},
            "0.0.1": {"name": "old.hdiff", "size": 2, "sha256": "d" * 64, "from_sha256": "e" * 64},
        },
    }
    release = {
        "tag_name": "v1.4.0",
        "html_url": "https://example/release",
        "assets": [
            {"name": "DSTCamp-1.4.0.exe", "browser_download_url": "https://example/exe"},
            {"name": "DSTCamp-1.4.0.sha256.json", "browser_download_url": "https://example/manifest"},
            {"name": "p.hdiff", "browser_download_url": "https://example/patch"},
        ],
    }
    with patch.object(update_check, "_request_json", return_value=manifest):
        parsed = update_check._parse_release(release, "gitee")
    assert parsed is not None and parsed.can_auto_update
    assert parsed.sha256 == "a" * 64 and parsed.size == 4
    # 只取以当前运行版本为起点的补丁
    assert parsed.patch == UpdatePatch("https://example/patch", 2, "b" * 64, "c" * 64)


def test_download_requires_matching_hash_and_size() -> None:
    payload = b"verified-update"
    release = UpdateRelease(
        "1.4.0",
        "https://example/release",
        "gitee",
        "https://example/exe",
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )
    with tempfile.TemporaryDirectory() as directory, patch.object(
        auto_update, "data_dir", return_value=Path(directory)
    ), patch.object(
        auto_update.urllib.request,
        "urlopen",
        side_effect=lambda *_args, **_kwargs: _Response(payload),
    ):
        path = auto_update.download_update(release)
        assert path.read_bytes() == payload
        # 已下载且校验一致时再次更新直接复用，不再联网
        with patch.object(auto_update.urllib.request, "urlopen", side_effect=AssertionError("不应重新下载")):
            assert auto_update.download_update(release) == path

        bad = UpdateRelease(
            release.version,
            release.page_url,
            release.source,
            release.exe_url,
            "0" * 64,
            release.size,
        )
        try:
            auto_update.download_update(bad)
        except OSError as exc:
            assert "SHA-256" in str(exc)
        else:
            raise AssertionError("错误哈希不应通过校验")


def test_download_resumes_and_falls_back_to_mirror() -> None:
    """主源断线时换备用源并按 Range 续传；服务器忽略 Range（如 Gitee 附件返回 200）时从头写。"""
    payload = bytes(range(256)) * 40
    release = UpdateRelease(
        "1.4.0", "https://example/release", "gitee", "https://primary/exe",
        hashlib.sha256(payload).hexdigest(), len(payload),
        fallback_urls=("https://mirror/exe",),
    )
    requested = []

    def urlopen(request, **_kwargs):
        requested.append((request.full_url, request.get_header("Range")))
        if request.full_url == "https://primary/exe":
            raise auto_update.urllib.error.URLError("断线")
        start = int(request.get_header("Range")[len("bytes="):-1])
        response = _Response(payload[start:])
        response.status = 206
        response.headers = {"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"}
        return response

    with tempfile.TemporaryDirectory() as directory, patch.object(
        auto_update, "data_dir", return_value=Path(directory)
    ):
        part = Path(directory) / "1.4.0" / "DSTCamp-1.4.0.exe.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(payload[:1000])
        with patch.object(auto_update.urllib.request, "urlopen", side_effect=urlopen):
            path = auto_update.download_update(release)
        assert path.read_bytes() == payload and not part.exists()
        assert requested == [("https://primary/exe", "bytes=1000-"), ("https://mirror/exe", "bytes=1000-")]

        path.unlink()
        part.write_bytes(b"x" * 1000)
        with patch.object(
            auto_update.urllib.request, "urlopen", side_effect=lambda *_a, **_k: _Response(payload)
        ):
            assert auto_update.download_update(release).read_bytes() == payload


def test_patch_update_builds_exe_locally() -> None:
    """本机 EXE 与补丁起点一致时只下补丁并用真实 hpatchz 合成；不一致时不下补丁，直接下整包。"""
    tool_dir = Path(__file__).resolve().parents[1] / "tools"
    old = bytes(range(256)) * 400
    new = old[:50000] + b"new-version-code" + old[50000:]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old_exe, new_exe, patch_file = root / "old.exe", root / "new.exe", root / "p.hdiff"
        old_exe.write_bytes(old)
        new_exe.write_bytes(new)
        subprocess.run([str(tool_dir / "hdiffpatch" / "hdiffz.exe"), "-f", str(old_exe), str(new_exe),
                        str(patch_file)], check=True, capture_output=True)
        patch_bytes = patch_file.read_bytes()
        update_patch = UpdatePatch("https://example/patch", len(patch_bytes),
                                   hashlib.sha256(patch_bytes).hexdigest(), hashlib.sha256(old).hexdigest())
        release = UpdateRelease("999.0.0", "https://example/release", "gitee", "https://example/exe",
                                hashlib.sha256(new).hexdigest(), len(new), patch=update_patch)
        requested = []

        def urlopen(request, **_kwargs):
            requested.append(request.full_url)
            return _Response(patch_bytes if request.full_url == update_patch.url else new)

        updates = root / "updates"
        with (
            patch.object(auto_update.sys, "frozen", True, create=True),
            patch.object(auto_update.sys, "executable", str(old_exe)),
            patch.object(auto_update, "data_dir", return_value=updates),
            patch.object(auto_update, "tool_binary_dir", return_value=tool_dir),
            patch.object(auto_update.urllib.request, "urlopen", side_effect=urlopen),
        ):
            assert auto_update.download_update(release).read_bytes() == new
            assert requested == [update_patch.url]

            requested.clear()
            (updates / "999.0.0" / "DSTCamp-999.0.0.exe").unlink()
            old_exe.write_bytes(b"user-built-exe")
            assert auto_update.download_update(release).read_bytes() == new
            assert requested == [release.exe_url]


def test_launch_helper_stages_on_exe_volume() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current = root / "DSTCamp-1.3.5.exe"
        staged = root / "cache" / "DSTCamp-1.3.6.exe"
        staged.parent.mkdir()
        current.write_bytes(b"old")
        staged.write_bytes(b"new")
        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update, "data_dir", return_value=root / "data"), patch.object(
            auto_update.subprocess, "Popen"
        ) as popen:
            auto_update.launch_update_helper(staged)
        command = popen.call_args.args[0]
        local_staged = Path(command[command.index("-NewExe") + 1])
        target = Path(command[command.index("-TargetExe") + 1])
        assert local_staged.parent.resolve() == current.parent.resolve()
        assert local_staged.read_bytes() == b"new"
        assert target.resolve() == (root / auto_update.STANDARD_EXE_NAME).resolve()
        assert (
            popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        )

        helper_script = Path(command[command.index("-File") + 1])
        helper_content = helper_script.read_text(encoding="utf-8-sig")
        reset_at = helper_content.index("PYINSTALLER_RESET_ENVIRONMENT")
        start_at = helper_content.index("Start-Process -FilePath $TargetExe")
        assert reset_at < start_at


def test_helper_migrates_over_existing_standard_exe() -> None:
    # 同目录已有旧的 DSTCamp.exe，又从带版本号的 EXE 更新：旧的固定名字备份成 .old 后被覆盖。
    # 真实跑 PowerShell 助手；Start-Process 需要合法 EXE，用系统小程序冒充各版本。
    system32 = Path(r"C:\Windows\System32")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / auto_update.STANDARD_EXE_NAME
        current = root / "DSTCamp-1.6.3.exe"
        staged = root / ".DSTCamp-1.6.3.update-1.exe"
        shutil.copy(system32 / "where.exe", target)
        shutil.copy(system32 / "hostname.exe", current)
        shutil.copy(system32 / "whoami.exe", staged)
        expected = {path: path.read_bytes() for path in (target, current, staged)}
        finished = subprocess.Popen(["cmd", "/c", "exit"], creationflags=subprocess.CREATE_NO_WINDOW)
        finished.wait()
        with patch.object(auto_update, "data_dir", return_value=root / "data"):
            script = auto_update._helper_script()
        subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-ParentPid", str(finished.pid), "-CurrentExe", str(current), "-NewExe", str(staged),
            "-TargetExe", str(target), "-BackupExe", str(root / "DSTCamp-1.6.3.exe.old"),
            "-TargetBackupExe", str(root / "DSTCamp.exe.old"),
        ], check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        assert not (root / "apply_update.log").exists()
        assert target.read_bytes() == expected[staged]
        assert (root / "DSTCamp.exe.old").read_bytes() == expected[target]
        assert (root / "DSTCamp-1.6.3.exe.old").read_bytes() == expected[current]
        time.sleep(1)  # 等拉起的进程退出再删临时目录


def test_standard_exe_name_is_fixed_but_custom_name_is_preserved() -> None:
    root = Path("C:/DSTCamp")
    staged = root / "cache" / "DSTCamp-1.3.6.exe"
    # 旧版本号命名一次性迁移到固定名字。
    assert auto_update.resolve_install_target(
        root / "DSTCamp-1.3.5.exe", staged
    ) == root / auto_update.STANDARD_EXE_NAME
    # 已经是固定名字的安装，后续更新继续保持固定名字。
    assert auto_update.resolve_install_target(
        root / auto_update.STANDARD_EXE_NAME, staged
    ) == root / auto_update.STANDARD_EXE_NAME
    # 用户自定义命名不受影响。
    assert auto_update.resolve_install_target(
        root / "我的开服工具.exe", staged
    ) == root / "我的开服工具.exe"


def test_cleanup_removes_known_stale_artifacts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        current = install_dir / "DSTCamp.exe"
        current.write_bytes(b"current")
        # 上一次成功更新留下的旧版本备份。
        (install_dir / "DSTCamp-1.3.7.exe.old").write_bytes(b"old")
        # 上一次失败更新留下的隐藏 staged 文件——用跟"当前文件名"不同的
        # stem 生成，模拟用户在两次更新之间改过名字的场景。
        (install_dir / ".我的旧名字.update-4321.exe").write_bytes(b"stale")
        (install_dir / "apply_update.log").write_text("失败原因", encoding="utf-8")
        keep_file = install_dir / "readme.txt"
        keep_file.write_text("keep", encoding="utf-8")

        updates_dir = root / "data" / "updates"
        (updates_dir / "1.3.6").mkdir(parents=True)
        (updates_dir / "1.3.6" / "DSTCamp-1.3.6.exe").write_bytes(b"stale-download")
        updates_dir.joinpath("apply_update.ps1").write_text("script", encoding="utf-8")
        # 比当前版本新的下载留给下次复用，半截文件留着续传
        newer = updates_dir / "999.0.0"
        newer.mkdir()
        (newer / "DSTCamp-999.0.0.exe").write_bytes(b"ready")
        (newer / "DSTCamp-999.0.0.exe.part").write_bytes(b"partial")

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update, "data_dir", return_value=updates_dir):
            auto_update.cleanup_stale_update_artifacts()

        assert not (install_dir / "DSTCamp-1.3.7.exe.old").exists()
        assert not (install_dir / ".我的旧名字.update-4321.exe").exists()
        assert not (install_dir / "apply_update.log").exists()
        assert keep_file.exists()
        assert current.exists()
        assert not (updates_dir / "1.3.6").exists()
        assert updates_dir.joinpath("apply_update.ps1").exists()
        assert (newer / "DSTCamp-999.0.0.exe").exists()
        assert (newer / "DSTCamp-999.0.0.exe.part").exists()


def test_cleanup_is_noop_when_not_frozen() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        stale = install_dir / "DSTCamp.exe.old"
        stale.write_bytes(b"old")
        with patch.object(auto_update.sys, "frozen", False, create=True), patch.object(
            auto_update.sys, "executable", str(install_dir / "DSTCamp.exe")
        ):
            auto_update.cleanup_stale_update_artifacts()
        assert stale.exists()


def test_cleanup_removes_vestigial_external_tools_once_embedded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        (install_dir / "tools").mkdir()
        (install_dir / "tools" / "leftover.txt").write_text("dead", encoding="utf-8")
        current = install_dir / "DSTCamp.exe"
        current.write_bytes(b"current")

        meipass = root / "meipass"
        (meipass / "tools").mkdir(parents=True)

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update.sys, "_MEIPASS", str(meipass), create=True):
            auto_update.cleanup_vestigial_external_tools()

        assert not (install_dir / "tools").exists()


def test_cleanup_keeps_external_tools_when_not_yet_embedded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        (install_dir / "tools").mkdir()
        needed = install_dir / "tools" / "still-needed.txt"
        needed.write_text("in use", encoding="utf-8")
        current = install_dir / "DSTCamp-1.3.5.exe"
        current.write_bytes(b"current")

        meipass = root / "meipass"
        meipass.mkdir()  # 没有内嵌 tools 子目录——这份 EXE 仍需要外置 tools

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update.sys, "_MEIPASS", str(meipass), create=True):
            auto_update.cleanup_vestigial_external_tools()

        assert needed.exists()


if __name__ == "__main__":
    run(globals())
