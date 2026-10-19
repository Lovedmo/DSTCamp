"""管理世界弹窗：列出专服存档的全部世界，改万界之门里的名称/隐藏/人数上限、改文件夹、删除世界。

每项修改都在后台先备份再写盘（业务见 features/world/shard_management.py），完成后重新扫描环境并刷新列表。
用内网穿透映射的世界不能改文件夹或删除：映射记录按分片名保存，先由用户在内网穿透页关闭。
"""

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QLineEdit, QPushButton, QWidget

from dstools.features.local_service.backup_manager import create_backup
from dstools.features.world import shard_management
from dstools.features.world.location_profiles import CAVES_SHARD, get_location_definition
from dstools.i18n import get_lang, t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.lan_mapping_guard import shard_mapping_name
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Banner
from dstools.shared import app_settings

MAX_PLAYER_LIMIT = 64  # cluster.ini max_players 的上限


class WorldEditDialog(dialogs.Dialog):
    """编辑一个世界在万界之门里的名称、是否隐藏和人数上限。"""

    def __init__(self, parent, cluster, entry: shard_management.WorldEntry):
        super().__init__(parent, t("world.manage_edit_title", shard=entry.shard_name), "md")
        self._cluster = cluster
        self._shard_name = entry.shard_name
        self.world_name, self.hidden, self.limit = entry.name, entry.hidden, entry.limit
        self.body.addWidget(self.text_label(t("world.add_shard_name_label")))
        self._name_edit = QLineEdit(entry.name or entry.shard_name)
        self._name_edit.setFont(theme.font("FONT_SIZE_BASE"))
        self.body.addWidget(self._name_edit)
        self.body.addWidget(self.text_label(t("world.add_shard_name_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self._hidden_check = QCheckBox(t("world.manage_hidden_label"))
        self._hidden_check.setFont(theme.font("FONT_SIZE_SM"))
        self._hidden_check.setChecked(entry.hidden)
        self.body.addSpacing(6)
        self.body.addWidget(self._hidden_check)
        self.body.addWidget(self.text_label(t("world.manage_hidden_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self.body.addSpacing(6)
        self.body.addWidget(self.text_label(t("world.manage_limit_label")))
        self._limit_edit = QLineEdit(str(entry.limit))
        self._limit_edit.setFixedWidth(90)
        self._limit_edit.setValidator(QIntValidator(0, MAX_PLAYER_LIMIT))
        self.body.addWidget(self._limit_edit)
        self.body.addWidget(self.text_label(t("world.manage_limit_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def accept_if_valid(self) -> None:
        world_name = self._name_edit.text().strip()
        error = shard_management.renamed_world_error(self._cluster, self._shard_name, world_name)
        if error:
            self._error.setText(error)
            return
        try:
            limit = int(self._limit_edit.text() or "0")
        except ValueError:
            limit = -1
        if not 0 <= limit <= MAX_PLAYER_LIMIT:
            self._error.setText(t("world.manage_limit_invalid"))
            return
        self.world_name, self.hidden, self.limit = world_name, self._hidden_check.isChecked(), limit
        self.accept()


class ManageWorldsDialog(dialogs.Dialog):
    """世界列表加每行的编辑/改文件夹/删除按钮；exec() 返回 ADD 表示用户点了「新增世界」。"""

    ADD = 2

    def __init__(self, parent, ctx):
        super().__init__(parent, t("world.manage_btn"), "lg")
        self.ctx = ctx
        self._busy = False
        self._intro = self.text_label("", size_key="FONT_SIZE_BASE")
        self.body.addWidget(self._intro)
        self._banner = Banner()
        self.body.addWidget(self._banner)
        host = QWidget()
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(0, 4, 0, 4)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(1, 1)  # 名称列吃掉多余宽度，按钮靠右
        self.body.addWidget(host)
        close = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close.clicked.connect(self.reject)
        close.setAutoDefault(False)
        self._add_button = QPushButton(t("world.add_shard_btn"))
        self._add_button.clicked.connect(lambda: self.done(self.ADD))
        self.add_footer([close], [self._add_button])
        self.reload()

    # ── 列表 ────────────────────────────────────────────────────────────
    def reload(self) -> None:
        while self._grid.count():
            widget = self._grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        cluster = self.ctx.selected_cluster()
        if cluster is None or cluster.source != SaveSource.SERVER:
            self.reject()
            return
        try:
            entries = shard_management.list_worlds(cluster)
            worldgate = shard_management.worldgate_enabled(cluster)
        except (OSError, ValueError) as exc:  # modoverrides.lua 或 server.ini 读不了
            self._intro.setText("")
            self._banner.set_text(str(exc))
            self._add_button.setEnabled(False)
            return
        running = self.ctx.cluster_running(cluster)
        self._intro.setText(t("world.manage_intro", cluster=cluster.name, count=len(entries)))
        self._banner.set_text(t("world.manage_running") if running else
                              "" if worldgate else t("world.manage_no_worldgate"))
        self._add_button.setEnabled(not running and not self._busy)
        for column, key in enumerate(("world.manage_col_folder", "world.manage_col_name",
                                      "world.manage_col_type", "world.manage_col_id")):
            self._grid.addWidget(self._label(t(key), muted=True), 0, column)
        editable = not running and not self._busy
        for row, entry in enumerate(entries, start=1):
            self._add_row(cluster, row, entry, editable, worldgate)

    def _label(self, text: str, muted: bool = False) -> QLabel:
        label = QLabel(text)
        label.setFont(theme.font("FONT_SIZE_SM"))
        label.setProperty("muted", muted)
        return label

    def _add_row(self, cluster, row: int, entry, editable: bool, worldgate: bool) -> None:
        tags = [entry.name or t("world.manage_unnamed")]
        if entry.is_master:
            tags.append(t("world.manage_master_tag"))
        if entry.hidden:
            tags.append(t("world.manage_hidden_tag"))
        if entry.limit:
            tags.append(t("world.manage_limit_tag", limit=entry.limit))
        self._grid.addWidget(self._label(entry.shard_name), row, 0)
        self._grid.addWidget(self._label(" · ".join(tags), muted=not entry.name), row, 1)
        self._grid.addWidget(self._label(self._location_text(entry.location)), row, 2)
        self._grid.addWidget(self._label(str(entry.shard_id) if entry.shard_id is not None else "-"), row, 3)

        edit = QPushButton(t("world.manage_edit_btn"))
        edit.setEnabled(editable and worldgate)
        edit.clicked.connect(lambda _c=False, e=entry: self._on_edit(e))
        folder_btn = QPushButton(t("world.manage_folder_btn"))
        protected = shard_management.rename_shard_error(cluster, entry.shard_name, entry.shard_name)
        folder_btn.setEnabled(editable and not protected)
        folder_btn.setToolTip(protected)
        folder_btn.clicked.connect(lambda _c=False, e=entry: self._on_rename_folder(e))
        delete = dialogs.style_button(QPushButton(t("world.manage_delete_btn")), "danger")
        delete.setEnabled(editable and not entry.is_master)
        delete.clicked.connect(lambda _c=False, e=entry: self._on_delete(e))
        for column, button in enumerate((edit, folder_btn, delete), start=4):
            button.setAutoDefault(False)
            self._grid.addWidget(button, row, column)

    @staticmethod
    def _location_text(location: str | None) -> str:
        if not location:
            return "-"
        try:
            return get_location_definition(location).name(get_lang())
        except ValueError:  # 没登记的 Mod 世界类型，直接显示原值
            return location

    # ── 操作 ────────────────────────────────────────────────────────────
    def _current(self, shard_name: str, mapping_key: str = ""):
        """操作前按最新状态再检查一次：存档已停止；mapping_key 非空时还要求这个世界没有内网穿透映射。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            return None
        if self.ctx.cluster_running(cluster):
            dialogs.show_warning(self, t("world.manage_btn"), t("world.manage_running"))
            self.reload()
            return None
        shard = next((s for s in cluster.shards if s.name == shard_name), None)
        if shard is None:
            self.reload()
            return None
        if mapping_key and (method := shard_mapping_name(self.ctx, cluster, shard)):
            dialogs.show_warning(self, t("world.manage_btn"), t(mapping_key, shard=shard_name, method=method))
            return None
        return cluster

    def _on_edit(self, entry) -> None:
        cluster = self._current(entry.shard_name)
        if cluster is None:
            return
        dialog = WorldEditDialog(self, cluster, entry)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted:
            return
        name, hidden, limit, language = dialog.world_name, dialog.hidden, dialog.limit, get_lang()

        def work() -> None:
            create_backup(cluster.path)
            shard_management.update_world_entry(cluster, entry.shard_name, name, hidden, limit, language)

        self._run(work, lambda: t("world.manage_saved"))

    def _on_rename_folder(self, entry) -> None:
        cluster = self._current(entry.shard_name, "world.manage_mapping_rename")
        if cluster is None:
            return
        old = entry.shard_name
        dialog = dialogs.TextInputDialog(
            self, t("world.manage_folder_title"), t("world.manage_folder_prompt", shard=old), old,
            validator=lambda value: shard_management.rename_shard_error(cluster, old, value))
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted or dialog.result_text in (None, old):
            return
        new = dialog.result_text

        def work() -> None:
            create_backup(cluster.path)
            shard_management.rename_shard_folder(cluster, old, new)

        def after() -> str:
            # 粘贴过的 Lolia 隧道来源按分片名保存（关闭映射后仍保留），跟着新名字走
            source = app_settings.get_lolia_source(cluster.path, old)
            if source is not None:
                app_settings.set_lolia_source(cluster.path, new, source)
                app_settings.set_lolia_source(cluster.path, old, None)
            return t("world.manage_renamed", shard=new)

        self._run(work, after)

    def _on_delete(self, entry) -> None:
        cluster = self._current(entry.shard_name, "world.manage_mapping_delete")
        if cluster is None:
            return
        shard_name = entry.shard_name
        text = t("world.manage_delete_confirm", name=entry.name or shard_name, shard=shard_name)
        if shard_name == CAVES_SHARD:
            text += t("world.manage_delete_caves_note")
        if not dialogs.ask_yes_no(self, t("world.manage_delete_title"), text, danger=True):
            return
        language = get_lang()

        def work() -> None:
            create_backup(cluster.path)
            shard_management.delete_shard(cluster, shard_name, language)

        def after() -> str:
            # 同名新世界不该继承已删世界粘贴过的隧道来源
            app_settings.set_lolia_source(cluster.path, shard_name, None)
            return t("world.manage_deleted", shard=shard_name)

        self._run(work, after)

    def _run(self, work, after) -> None:
        """后台执行 work；成功后在界面线程调用 after() 取完成提示，重新扫描环境并刷新列表。"""
        self._busy = True
        self.reload()

        def done(_result) -> None:
            self._busy = False
            message = after()
            self.ctx.refresh_env()
            self.reload()
            dialogs.show_info(self, t("world.manage_btn"), message)

        def failed(exc: Exception) -> None:
            self._busy = False
            self.reload()
            dialogs.show_error(self, t("world.manage_btn"), str(exc))

        run_async(work, done, failed)
