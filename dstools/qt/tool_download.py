"""按需工具（shared/tool_store.py）的下载界面：日志窗口里显示进度，成功后自动关窗并继续原来的操作。"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Callable

from PySide6.QtGui import QGuiApplication

from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.threads import post_to_ui, run_async
from dstools.shared import tool_store


def download_tool_with_dialog(parent, relative: str, title: str, on_done: Callable[[Path], None]) -> None:
    """后台下载 relative 登记的工具；失败时窗口留着，询问是否打开蓝奏云下载网盘工具包再导入。"""
    tool = tool_store.store_tool(relative)
    dialog = dialogs.LogDialog(parent, title)
    dialog.append(t("tools.download_start", name=tool.asset, size=f"{tool.size / 1e6:.1f}"), "section")
    dialog.show()
    shown = [0]

    def progress(done: int, total: int) -> None:
        percent = done * 100 // max(1, total)
        if percent >= shown[0] + 20:
            shown[0] = percent - percent % 20
            post_to_ui(lambda value: dialog.append(t("tools.download_progress", percent=value), "detail"), shown[0])

    def finished(path: Path) -> None:
        dialog.finish()
        dialog.accept()
        on_done(path)

    def failed(exc: Exception) -> None:
        dialog.append(t("tools.download_failed", error=exc), "result_error")
        dialog.append(t("tools.download_offline_hint", package=tool_store.TOOLS_PACKAGE_NAME), "note")
        dialog.finish()
        if dialogs.ask_yes_no(dialog, title, t("tools.open_lanzou_confirm", password=tool_store.TOOLS_LANZOU_PASSWORD)):
            # 先复制密码再跳转，免得用户在蓝奏页和程序之间来回找
            QGuiApplication.clipboard().setText(tool_store.TOOLS_LANZOU_PASSWORD)
            dialogs.show_toast(dialog, t("tools.lanzou_password_copied"))
            webbrowser.open(tool_store.TOOLS_LANZOU_URL)

    run_async(lambda: tool_store.download_tool(relative, progress), finished, failed)
