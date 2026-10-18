"""Mod 页"按 ID 下载"：输入工坊 ID 或链接，按当前 Steam 通道下载。

SteamCMD 通道交给 steamcmd_mods.run_steamcmd_mod_update；Steam 客户端通道在这里以当前账号订阅后下载。
"""

from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from dstools.features.mod.workshop_webapi import parse_workshop_ids
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async_with_log

_PREVIEW_IDS = 8  # 识别结果最多列出的 ID 数，其余用"……"代替


class WorkshopDownloadDialog(dialogs.Dialog):
    """多行输入框，边输入边显示识别到的 ID；确认后 workshop_ids 为要下载的 ID。"""

    def __init__(self, parent, *, steamcmd: bool):
        super().__init__(parent, t("mod.download_by_id_title"), width="md")
        self.workshop_ids: list[str] = []
        self.body.addWidget(self.text_label(t("mod.download_by_id_prompt"), size_key="FONT_SIZE_MD"))
        self._edit = QPlainTextEdit()
        self._edit.setFont(theme.font("FONT_SIZE_SM"))
        self._edit.setPlaceholderText("https://steamcommunity.com/sharedfiles/filedetails/?id=3444078585")
        self._edit.setFixedHeight(96)
        self._edit.textChanged.connect(self._on_text_changed)
        self.body.addWidget(self._edit)
        self._parsed = self.text_label("", muted=True)
        self.body.addWidget(self._parsed)
        self._invalid = self.error_label()
        self.body.addWidget(self._invalid)
        self.body.addWidget(self.text_label(
            t("mod.download_by_id_hint_steamcmd" if steamcmd else "mod.download_by_id_hint_client"), muted=True))
        cancel = dialogs.style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        cancel.clicked.connect(self.reject)
        self._confirm = QPushButton(t("mod.recommend_download"))
        self._confirm.clicked.connect(self.accept_if_valid)
        self.add_footer([cancel], [self._confirm])
        self._on_text_changed()
        self._edit.setFocus()

    def _on_text_changed(self) -> None:
        ids, invalid = parse_workshop_ids(self._edit.toPlainText())
        shown = "、".join(ids[:_PREVIEW_IDS]) + ("……" if len(ids) > _PREVIEW_IDS else "")
        self._parsed.setText(t("mod.download_by_id_parsed", count=len(ids), ids=shown) if ids else "")
        self._parsed.setVisible(bool(ids))
        self._invalid.setText(t("mod.download_by_id_invalid", items="、".join(invalid)) if invalid else "")
        self._invalid.setVisible(bool(invalid))
        self._confirm.setEnabled(bool(ids))
        self.workshop_ids = ids

    def accept_if_valid(self) -> None:
        if self.workshop_ids:
            self.accept()


def _label(workshop_id: str, title: str) -> str:
    return f"workshop-{workshop_id}" + (f"（{title}）" if title else "")


def run_subscribe_and_download(parent, workshop_ids, *, on_finished=None) -> None:
    """Steam 客户端通道：先用 Web API 排除不存在/不是饥荒的 ID，再以当前账号订阅并等待下载完成。"""
    ids = [str(w) for w in dict.fromkeys(workshop_ids) if str(w).isdigit()]
    dialog = dialogs.LogDialog(parent, t("mod.download_by_id_title"))
    dialogs.fit_to_screen(dialog, dialogs.DIALOG_WIDTHS["lg"], 520)
    dialog.show()

    def work(emit):
        from dstools.features.mod.workshop_api import subscribe_workshop_items, update_workshop_items
        from dstools.features.mod.workshop_webapi import fetch_remote_items
        emit(t("mod.download_by_id_checking", count=len(ids)))
        titles: dict[str, str] = {}
        targets = ids
        try:
            remote = fetch_remote_items(ids)
        except (OSError, ValueError) as exc:  # 网络或返回内容异常不拦下载，订阅结果会说明 ID 是否有效
            emit(t("mod.download_by_id_check_failed", detail=str(exc)))
        else:
            titles = {wid: item.title for wid, item in remote.items()}
            targets = [wid for wid in ids if wid in remote]
            unavailable = [wid for wid in ids if wid not in remote]
            if unavailable:
                emit(t("steamcmd_mods.unavailable", mods="、".join(f"workshop-{wid}" for wid in unavailable)))
        if not targets:
            return 0
        for wid in targets:
            emit(f"· {_label(wid, titles.get(wid, ''))}")
        emit(t("local.missing_mods_subscribing", count=len(targets)))
        subscribed = subscribe_workshop_items([int(wid) for wid in targets])
        ok_ids = [wid for wid, error in subscribed.items() if not error]
        for wid, error in subscribed.items():
            if error:
                emit(t("local.missing_mods_subscribe_failed_line", id=wid, error=error))
        if not ok_ids:
            return 0
        emit(t("local.missing_mods_downloading", count=len(ok_ids)))
        batch = update_workshop_items(ok_ids, on_item_complete=lambda current, total, result: emit(
            f"[{current}/{total}] {_label(str(result.workshop_id), titles.get(str(result.workshop_id), ''))}："
            + (t("local.missing_mods_item_ok") if result.completed
               else (result.error or t("local.missing_mods_item_failed")))))
        return batch.succeeded

    def finish() -> None:
        dialog.finish()
        if on_finished is not None:
            on_finished()  # 中途出错时也可能已有部分 Mod 下载完成

    def done(ready: int) -> None:
        dialog.append(t("mod.download_by_id_done", ready=ready, total=len(ids)),
                      "result_success" if ready == len(ids) else "result_warning")
        finish()

    def error(exc: Exception) -> None:
        dialog.append(t("local.missing_mods_error", detail=f"{type(exc).__name__}: {exc}"), "result_error")
        finish()

    run_async_with_log(work, dialog.append, done, error)
