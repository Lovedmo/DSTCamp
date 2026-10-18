"""自定义背景图下直接压在背景上的文字：只改文字本身，背景一点不动。

所有主题都是浅底深字，偏暗的图会吃掉深色文字。按整窗实际显示的平均背景明暗统一二选一（不按局部，免得同页深浅混杂）：
- 背景偏暗：正文、次要文字这类低饱和深色字换成明度翻转的浅色字（主次层级不变），下面垫一层很淡的深色柔影；
- 背景偏亮或是状态色文字：保持原色，深色字快看不清时外沿才渐显一圈很淡的底色描边（字够清楚时不画，免得像发光）。
标签、透明底按钮等用 QGraphicsEffect 实现（控件背景透明，源图只有字形，可整体换色或错开叠成描边）；
自绘文字的控件（页签条）调用 draw_adaptive_text。只在背景图生效时给登记窗口里的控件装上，关掉时卸掉。
"""

import math
import weakref

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QCheckBox, QGraphicsEffect, QLabel, QPushButton, QRadioButton, QWidget

from dstools.qt.theme import theme

_RADIUS = 1.0  # 描边宽度（逻辑像素），再宽中文笔画会糊在一起
_STEPS = 12  # 一圈错开的方向数，越多描边越圆
_LAYERS = 2  # 柔化后光晕变淡，叠几层补足浓度
_SHADOW_ALPHA = 110  # 浅色字下面深色柔影的浓度（0~255，再叠 _LAYERS 层）
# 深色字外沿的底色描边按需渐显：正文对比度不低于 _HALO_FROM 时不画（字本身够清楚，描边在偏暗背景上
# 反而像发光），降到 _HALO_FULL 时画满；再暗就整体换浅色字（_wants_light 的切换点在对比度 3 左右）
_HALO_FROM = 4.5
_HALO_FULL = 3.0


def _luminance(color: QColor) -> float:
    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * linear(color.redF()) + 0.7152 * linear(color.greenF()) + 0.0722 * linear(color.blueF())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _is_neutral_dark(color: QColor) -> bool:
    """主题正文、次要文字这类低饱和深色字；橙色"未就绪"、绿色成功等状态色不算，保留原色。"""
    return _luminance(color) < 0.3 and color.hslSaturationF() < 0.5


def halo_strength(fg: QColor, backdrop: QColor) -> float:
    """深色字底色描边的浓度（0~1）：按 fg 在整窗平均背景上的对比度，从 _HALO_FROM 到 _HALO_FULL 线性渐显。"""
    ratio = _contrast(fg, backdrop)
    return min(1.0, max(0.0, (_HALO_FROM - ratio) / (_HALO_FROM - _HALO_FULL)))


def _light_variant(color: QColor) -> QColor:
    """深色字对应的浅色字：明度翻转，正文更亮、次要文字稍暗，主次层级不变。"""
    h, s, l, _ = color.getHslF()
    return QColor.fromHslF(max(h, 0.0), s * 0.4, 1 - l * 0.55)


def _tinted(source: QPixmap, color: QColor) -> QPixmap:
    out = QPixmap(source)
    painter = QPainter(out)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(out.rect(), color)
    painter.end()
    return out


_manager_ref = None  # 当前的 TextHaloManager（弱引用），供自绘文字的控件查询


def light_text_for(widget: QWidget, fg: QColor) -> QColor | None:
    """背景图整体偏暗时，widget 的深色字 fg 返回对应浅色；否则（含无背景图、状态色）返回 None。
    整窗统一判断（按正文色在整窗平均背景上的对比度），不按局部区域，免得同一页一半深字一半浅字。"""
    manager = _manager_ref() if _manager_ref else None
    if (manager is None or not manager.enabled or widget.window() not in manager.windows
            or not manager.light_mode or not _is_neutral_dark(fg)):
        return None
    return _light_variant(fg)


def draw_adaptive_text(painter: QPainter, widget: QWidget, rect: QRectF, flags, text: str, fg: QColor) -> None:
    """自绘控件画直接压在背景上的文字：背景图下按底下明暗换浅色字加淡阴影，或保持深色字加淡描边。"""
    manager = _manager_ref() if _manager_ref else None
    if manager is None or not manager.enabled or widget.window() not in manager.windows:
        painter.setPen(fg)
        painter.drawText(rect, flags, text)
        return
    light = light_text_for(widget, fg)
    halo = (QColor(0, 0, 0, _SHADOW_ALPHA // 3) if light is not None
            else _alpha(theme.color("BG_SOFT"), round(90 * manager.halo_strength)))
    if halo.alpha() > 0:
        painter.setPen(halo)
        for i in range(8):
            angle = math.pi * i / 4
            painter.drawText(rect.translated(math.cos(angle), math.sin(angle)), flags, text)
    painter.setPen(light if light is not None else fg)
    painter.drawText(rect, flags, text)


def _alpha(color: QColor, alpha: int) -> QColor:
    out = QColor(color)
    out.setAlpha(alpha)
    return out


class TextHaloEffect(QGraphicsEffect):
    def __init__(self, widget: QWidget):
        super().__init__(widget)
        self._widget = widget

    def boundingRectFor(self, rect):
        pad = math.ceil(_RADIUS) + 2  # 柔化会再往外晕开一点
        return rect.adjusted(-pad, -pad, pad, pad)

    def draw(self, painter: QPainter) -> None:
        # PySide6 的 offset 是出参：传入 QPoint 原地填写；源图每次都是新对象，描边现算（十几次小图贴图）
        offset = QPoint()
        source = self.sourcePixmap(Qt.CoordinateSystem.LogicalCoordinates, offset,
                                   QGraphicsEffect.PixmapPadMode.PadToEffectiveBoundingRect)
        if source.isNull():
            return
        widget = self._widget
        # 源图里只有文字时才能整体换色：纯文字标签、未悬停未按下的透明底按钮（复选框源图里还有指示器）
        tintable = isinstance(widget, QLabel) or (
            isinstance(widget, QPushButton) and not widget.underMouse() and not widget.isDown())
        light = light_text_for(widget, widget.palette().color(widget.foregroundRole())) if tintable else None
        if light is not None:
            glyphs = _tinted(source, light)
            painter.drawPixmap(offset, self._make_halo(glyphs, QColor(0, 0, 0, _SHADOW_ALPHA)))
            painter.drawPixmap(offset, glyphs)
            return
        manager = _manager_ref() if _manager_ref else None
        strength = manager.halo_strength if manager is not None else 1.0
        if strength > 0:
            painter.drawPixmap(offset, self._make_halo(source, _alpha(theme.color("BG_SOFT"), round(255 * strength))))
        painter.drawPixmap(offset, source)

    @staticmethod
    def _make_halo(source: QPixmap, color) -> QPixmap:
        """字形往四周错开叠成一圈并染色，再缩小一半放大回来柔化成贴字的光晕，叠两层补足浓度。"""
        ring = QPixmap(source.size())
        ring.setDevicePixelRatio(source.devicePixelRatio())
        ring.fill(Qt.GlobalColor.transparent)
        painter = QPainter(ring)
        for i in range(_STEPS):
            angle = 2 * math.pi * i / _STEPS
            painter.drawPixmap(QPointF(_RADIUS * math.cos(angle), _RADIUS * math.sin(angle)), source)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(ring.rect(), color)
        painter.end()
        smooth = Qt.TransformationMode.SmoothTransformation
        soft = ring.scaled(max(1, ring.width() // 2), max(1, ring.height() // 2),
                           Qt.AspectRatioMode.IgnoreAspectRatio, smooth)
        soft = soft.scaled(ring.size(), Qt.AspectRatioMode.IgnoreAspectRatio, smooth)
        halo = QPixmap(ring.size())
        halo.setDevicePixelRatio(ring.devicePixelRatio())
        halo.fill(Qt.GlobalColor.transparent)
        painter = QPainter(halo)
        soft.setDevicePixelRatio(ring.devicePixelRatio())
        for _ in range(_LAYERS):
            painter.drawPixmap(0, 0, soft)
        painter.end()
        return halo


def _wants_halo(widget: QWidget) -> bool:
    """直接压在背景上的文字控件；自带底色的按钮、输入框和图片标签不描边。"""
    if isinstance(widget, QLabel):
        if widget.pixmap() is not None and not widget.pixmap().isNull():
            return False
        sheet = widget.styleSheet()
        return not ("background" in sheet and "transparent" not in sheet)  # 自带底色的标签（徽标等）
    if isinstance(widget, (QCheckBox, QRadioButton)):
        return True
    if isinstance(widget, QPushButton):
        return widget.isFlat() or widget.property("flat") in (True, "true") or widget.property("variant") == "secondary"
    return bool(getattr(widget, "wants_text_halo", False))  # 自绘文字的控件自行声明


class TextHaloManager(QObject):
    """背景图生效时，给登记窗口里的文字控件装描边；之后显示出来的新控件也补装（应用级 Show 事件）。"""

    def __init__(self, background):
        global _manager_ref
        super().__init__()
        self.background = background
        self.windows: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self.enabled = False
        self.light_mode = False  # 整窗背景偏暗、深色字统一换浅色
        self.halo_strength = 0.0  # 深色字底色描边的浓度，随整窗背景明暗渐显
        self._pending: list[weakref.ref] = []
        QApplication.instance().installEventFilter(self)
        theme.changed.connect(self.refresh)  # 底色变了，整窗深浅也要重判
        _manager_ref = weakref.ref(self)

    def watch(self, window: QWidget) -> None:
        self.windows.add(window)
        if self.enabled:
            self._apply(window, True)

    def refresh(self) -> None:
        """背景图、不透明度或主题变化后调用：重新判断整窗深浅字；开关状态变了才遍历控件。"""
        enabled = self.background.active
        self.light_mode = enabled and self._wants_light()
        self.halo_strength = halo_strength(theme.color("TEXT"), self.background.backdrop_color()) if enabled else 0.0
        if enabled == self.enabled:
            return
        self.enabled = enabled
        for window in list(self.windows):
            self._apply(window, enabled)

    def _wants_light(self) -> bool:
        """正文色的浅色版在整窗平均背景上比原色更清楚；带一点回差，拖不透明度时不在临界点来回闪。"""
        backdrop = self.background.backdrop_color()
        fg = theme.color("TEXT")
        ratio = _contrast(_light_variant(fg), backdrop) / _contrast(fg, backdrop)
        return ratio > (0.9 if self.light_mode else 1.1)

    def _apply(self, window: QWidget, enabled: bool) -> None:
        for widget in window.findChildren(QWidget):
            effect = widget.graphicsEffect()
            if enabled and effect is None and _wants_halo(widget):
                widget.setGraphicsEffect(TextHaloEffect(widget))
            elif not enabled and isinstance(effect, TextHaloEffect):
                widget.setGraphicsEffect(None)

    def eventFilter(self, obj, event) -> bool:
        # 在 Show 事件里直接 setGraphicsEffect 会让 Qt 访问越界崩溃，记下来等本轮事件处理完再装
        if event.type() == QEvent.Type.Show and self.enabled and isinstance(obj, QWidget) and _wants_halo(obj):
            if not self._pending:
                QTimer.singleShot(0, self._install_pending)
            self._pending.append(weakref.ref(obj))
        return False

    def _install_pending(self) -> None:
        pending, self._pending = self._pending, []
        if not self.enabled:
            return
        for ref in pending:
            widget = ref()
            if (widget is not None and shiboken6.isValid(widget) and widget.graphicsEffect() is None
                    and widget.window() in self.windows):
                widget.setGraphicsEffect(TextHaloEffect(widget))
