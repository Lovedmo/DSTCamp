"""SteamCMD 通道「Mod 全局更新」：列出 SteamCMD 工坊目录里已下载的和任一服务器存档用到的工坊 Mod，
对照 Web API 标出未下载 / 有更新 / 最新 / 工坊查不到，V1、V2 都按清单更新时间判断（已实测两者都与 Web API 一致）。

「一键更新」只更新已下载且工坊有新版本的；没下载过的由「下载缺失」单独处理，免得一次拉下几十个不用的 Mod。
V1 包下载后，专服 mods 里的展开副本在下次启动前自动部署，这里只提示。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem

from dstools.features.mod.steamcmd_mod_status import SteamcmdModRow, SteamcmdModState, scan_steamcmd_mods
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import PillTabBar

_STATE_KEYS = {
    SteamcmdModState.OUTDATED: "steamcmd_update.state_outdated",
    SteamcmdModState.MISSING: "steamcmd_update.state_missing",
    SteamcmdModState.CURRENT: "steamcmd_update.state_current",
    SteamcmdModState.UNAVAILABLE: "steamcmd_update.state_unavailable",
}
_STATE_ORDER = {SteamcmdModState.OUTDATED: 0, SteamcmdModState.MISSING: 1,
                SteamcmdModState.CURRENT: 2, SteamcmdModState.UNAVAILABLE: 3}
_FILTER_ALL, _FILTER_PENDING, _FILTER_CURRENT_SAVE = range(3)


class SteamcmdModUpdateDialog(dialogs.Dialog):
    def __init__(self, parent, clusters, server_mods_root, current_ids, on_changed=None):
        super().__init__(parent, t("steamcmd_update.title"), "lg", confirm_text=t("mod.update_selected_btn"))
        self._clusters = clusters
        self._server_mods_root = server_mods_root
        self._current_ids = set(current_ids)
        self._on_changed = on_changed
        self._rows: list[SteamcmdModRow] = []
        self._checked: set[str] = set()
        self._loading = False

        self.body.addWidget(self.text_label(t("steamcmd_update.desc"), muted=True))
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(t("mod.filter")))
        self._search = QLineEdit()
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._render)
        toolbar.addWidget(self._search)
        self._filter = PillTabBar(
            [t("mod.show_all"), t("mod.update_filter_needs_update"), t("mod.update_filter_current")],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=3, pad=18, uniform_width=True)
        self._filter.current_changed.connect(lambda _i: self._render())
        toolbar.addWidget(self._filter)
        self._refresh_btn = QPushButton(t("mod.update_refresh_states"))
        self._refresh_btn.clicked.connect(self._reload)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addStretch()
        self._count_label = self.text_label("", muted=True, wrap=False)
        toolbar.addWidget(self._count_label)
        self.body.addLayout(toolbar)

        self._tree = QTreeWidget()
        self._tree.setFont(theme.font("FONT_SIZE_SM"))
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setHeaderLabels([t("steamcmd_update.col_name"), t("steamcmd_update.col_id"),
                                    t("steamcmd_update.col_format"), t("steamcmd_update.col_state"),
                                    t("steamcmd_update.col_used_by")])
        self._tree.itemChanged.connect(self._on_item_changed)
        self.body.addWidget(self._tree, 1)

        self._missing_btn = dialogs.style_button(QPushButton(), "secondary")
        self._missing_btn.clicked.connect(lambda: self._download(self._ids_in(SteamcmdModState.MISSING), confirm=True))
        self._quick_btn = QPushButton()
        self._quick_btn.clicked.connect(lambda: self._download(self._ids_in(SteamcmdModState.OUTDATED)))
        self._confirm = QPushButton(self._confirm_text)
        self._confirm.clicked.connect(self.accept_if_valid)
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.reject)
        for button in (close_btn, self._missing_btn, self._quick_btn, self._confirm):
            button.setAutoDefault(False)
        self.add_footer([close_btn], [self._missing_btn, self._quick_btn, self._confirm])
        dialogs.fit_to_screen(self, dialogs.DIALOG_WIDTHS["lg"] + 160, 620)
        self._reload()

    # ── 数据 ────────────────────────────────────────────────────────────
    def _reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._set_busy(True)
        self._count_label.setText(t("mod.update_status_checking_hint"))
        clusters, root = self._clusters, self._server_mods_root
        run_async(lambda: scan_steamcmd_mods(clusters, root), self._on_scanned, self._on_scan_failed)

    def _on_scanned(self, rows) -> None:
        self._loading = False
        self._rows = sorted(rows, key=lambda r: (_STATE_ORDER[r.status.state], (r.name or r.workshop_id).casefold()))
        downloadable = {r.workshop_id for r in self._rows if r.status.needs_download}
        self._checked &= downloadable  # 刷新后已更新好的取消勾选
        self._set_busy(False)
        self._render()

    def _on_scan_failed(self, exc: Exception) -> None:
        self._loading = False
        self._set_busy(False)
        self._count_label.setText(t("steamcmd_mods.check_failed", detail=str(exc)))

    def _ids_in(self, state: SteamcmdModState) -> list[str]:
        return [r.workshop_id for r in self._rows if r.status.state is state]

    def _visible_rows(self) -> list[SteamcmdModRow]:
        needle = self._search.text().strip().casefold()
        mode = self._filter.current_index()
        result = []
        for row in self._rows:
            if mode == _FILTER_PENDING and not row.status.needs_download:
                continue
            if mode == _FILTER_CURRENT_SAVE and row.workshop_id not in self._current_ids:
                continue
            if needle and needle not in f"{row.name} {row.workshop_id}".casefold():
                continue
            result.append(row)
        return result

    # ── 界面 ────────────────────────────────────────────────────────────
    def _set_busy(self, busy: bool) -> None:
        self._refresh_btn.setEnabled(not busy)
        self._tree.setEnabled(not busy)
        self._refresh_buttons(busy)

    def _refresh_buttons(self, busy: bool = False) -> None:
        missing, outdated = len(self._ids_in(SteamcmdModState.MISSING)), len(self._ids_in(SteamcmdModState.OUTDATED))
        self._missing_btn.setText(t("steamcmd_update.download_missing_btn", count=missing))
        self._quick_btn.setText(t("steamcmd_update.quick_btn", count=outdated))
        self._missing_btn.setEnabled(not busy and missing > 0)
        self._quick_btn.setEnabled(not busy and outdated > 0)
        self._confirm.setEnabled(not busy and bool(self._checked))

    @staticmethod
    def _state_text(row: SteamcmdModRow) -> str:
        text = t(_STATE_KEYS[row.status.state])
        if row.v1_deploy_pending and row.status.state is not SteamcmdModState.MISSING:
            text += t("steamcmd_update.v1_deploy_pending")
        return text

    def _render(self, *_args) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        for row in self._visible_rows():
            used = "、".join(row.used_by) if row.used_by else t("steamcmd_update.used_by_none")
            item = QTreeWidgetItem([row.name or f"workshop-{row.workshop_id}", row.workshop_id,
                                    row.mod_format or "-", self._state_text(row), used])
            item.setData(0, Qt.ItemDataRole.UserRole, row.workshop_id)
            item.setToolTip(4, used)
            if row.status.needs_download:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if row.workshop_id in self._checked
                                   else Qt.CheckState.Unchecked)
            else:
                # QTreeWidgetItem 默认带可勾选标志：不需要下载的去掉，免得被当成可选项
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            if row.status.state is SteamcmdModState.OUTDATED:
                item.setForeground(3, theme.color("PRIMARY"))
            elif row.status.state is SteamcmdModState.UNAVAILABLE:
                item.setForeground(3, theme.color("ERROR"))
            self._tree.addTopLevelItem(item)
        self._tree.blockSignals(False)
        for column in range(4):
            self._tree.resizeColumnToContents(column)
        pending = sum(1 for r in self._rows if r.status.needs_download)
        if not self._loading:
            self._count_label.setText(t("steamcmd_update.count", total=len(self._rows), pending=pending))
        self._refresh_buttons(self._loading)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        wid = item.data(0, Qt.ItemDataRole.UserRole)
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked.add(wid)
        else:
            self._checked.discard(wid)
        self._refresh_buttons()

    # ── 操作 ────────────────────────────────────────────────────────────
    def accept_if_valid(self) -> None:
        self._download(sorted(self._checked, key=int))

    def _download(self, ids: list[str], confirm: bool = False) -> None:
        if not ids:
            return
        if confirm and not dialogs.ask_yes_no(self, t("steamcmd_update.title"),
                                              t("steamcmd_update.download_missing_confirm", count=len(ids))):
            return
        from dstools.features.local_service import steamcmd_updater
        from dstools.qt.steamcmd_mods import run_steamcmd_mod_update
        if steamcmd_updater.find_steamcmd() is None:
            run_steamcmd_mod_update(self, ids)  # 只会提示 SteamCMD 不可用的原因
            return
        self._set_busy(True)

        def finished() -> None:
            if self._on_changed is not None:
                self._on_changed()
            self._reload()  # 以复查结果为准刷新列表

        run_steamcmd_mod_update(self, ids, on_finished=finished)
