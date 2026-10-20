"""生成按需下载工具的发布文件（工具版本变化、升 TOOLS_RELEASE_TAG 时才需要运行）。

- release/<标签>/：上传到 GitHub 上同名标签的 Release 作为附件（再用 sync_gitee_releases.ps1 同步到 Gitee）；
- release/offline/DSTCamp-<标签>.zip：传到蓝奏云等网盘的工具包，用户在"关于 → 手动更新 → 从文件导入"导入。

文件取自 tools/，逐个核对 dstools/shared/tool_store.py 里登记的大小和 SHA-256。
build_exe.py 会清空 release/，所以先构建 EXE，再运行本脚本。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dstools.shared import tool_store  # noqa: E402

# 随工具一起分发的许可证（不登记、导入时忽略）
_LICENSE_FILES = {"frp_selfhost/LICENSE": "frp-LICENSE.txt"}


def main() -> None:
    tag = tool_store.TOOLS_RELEASE_TAG
    release_dir = PROJECT_ROOT / "release" / tag
    shutil.rmtree(release_dir, ignore_errors=True)
    release_dir.mkdir(parents=True)
    for tool in tool_store.TOOLS:
        source = PROJECT_ROOT / "tools" / tool.relative
        data = source.read_bytes()
        if len(data) != tool.size or hashlib.sha256(data).hexdigest() != tool.sha256:
            raise SystemExit(f"{source} 与 tool_store.py 的登记不一致，先更新登记")
        (release_dir / tool.asset).write_bytes(data)
    for relative, name in _LICENSE_FILES.items():
        shutil.copy2(PROJECT_ROOT / "tools" / relative, release_dir / name)

    package = PROJECT_ROOT / "release" / "offline" / tool_store.TOOLS_PACKAGE_NAME
    package.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(release_dir.iterdir()):
            archive.write(path, path.name)
    print(f"工具 Release 附件（上传到 GitHub 标签 {tag}）：{release_dir}")
    print(f"网盘工具包：{package}（{package.stat().st_size / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
