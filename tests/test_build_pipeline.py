"""固定发布资源、构建暂存和校验清单测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from scripts import build_exe
from _harness import run  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_fixed_resource_layout_matches_build_manifest() -> None:
    actual_tools = {
        path.relative_to(PROJECT_ROOT / "tools").as_posix()
        for path in (PROJECT_ROOT / "tools").rglob("*")
        if path.is_file()
    }
    assert actual_tools == (set(build_exe.TOOL_FILES) | set(build_exe.BUILD_ONLY_TOOL_FILES)
                            | set(build_exe.ON_DEMAND_TOOL_FILES))

    # 按需下载工具的登记（大小、SHA-256）与仓库文件一致，不打包的正好是 bundled=False 的那些
    from dstools.shared import tool_store

    for tool in tool_store.TOOLS:
        assert build_exe._file_info(PROJECT_ROOT / "tools" / tool.relative) == {
            "size": tool.size, "sha256": tool.sha256}, tool.relative
    assert set(build_exe.ON_DEMAND_TOOL_FILES) == {tool.relative for tool in tool_store.TOOLS if not tool.bundled}

    for relative in build_exe.REQUIRED_ICON_FILES:
        assert (PROJECT_ROOT / "icons" / relative).is_file(), relative

    assert not any(
        (PROJECT_ROOT / name).is_dir()
        for name in ("cache", "data", "security")
    ), "运行时可写目录不应作为仓库固定资源存在"


def test_staging_copies_only_declared_runtime_resources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        staged_tools = build_exe._stage_tools(PROJECT_ROOT, root)
        staged_icons = build_exe._stage_icons(PROJECT_ROOT, root)

        copied_tools = {
            path.relative_to(staged_tools).as_posix()
            for path in staged_tools.rglob("*")
            if path.is_file()
        }
        assert copied_tools == set(build_exe.TOOL_FILES)
        assert not (staged_icons / "world" / "README.txt").exists()
        assert not (staged_icons / "ui" / "open_file_folder_fluent_LICENSE.txt").exists()


def test_sha256_manifest_matches_artifacts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        exe = root / "DSTCamp-test.exe"
        exe.write_bytes(b"exe-content")
        manifest_path = root / "DSTCamp-test.sha256.json"

        build_exe._write_sha256_manifest(manifest_path, "test", (exe,))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["version"] == "test"
        entry = manifest["files"][exe.name]
        assert entry["size"] == exe.stat().st_size
        assert entry["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()


if __name__ == "__main__":
    run(globals())
