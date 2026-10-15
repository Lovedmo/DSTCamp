"""自定义背景图：主窗口 paintEvent 整窗只画一次（底色 + 居中裁剪填满的图片按不透明度叠加），子控件透明透出。

所有主题都是浅底深字，图里偏暗的区域会吃掉直接画在背景上的文字。只在文字附近"保字"：
加载时另备一份提亮图——相对亮度低于"次要文字 TEXT_MUTED 仍有 3:1 对比度"的像素，按刚好够用的比例
往底色 BG_SOFT 混合，已经够亮的像素原样保留；绘制时把调用方给的文字区域外扩、羽化成遮罩，
遮罩内换成提亮图（同样按不透明度叠加），遮罩外照常显示原图，不透明度滑块含义不变。
换主题时底色和文字色变了，需要 reload()。

缩放过程中用快速变换，停顿后再用平滑变换重画。
"""

import math
import weakref

from PIL import Image, ImageChops, ImageFilter
from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton, QRadioButton, QWidget,
)

from dstools.qt.theme import theme
from dstools.shared.custom_background import get_custom_bg_path
from dstools.shared.app_settings import get_custom_bg_opacity

_MAX_WIDTH = 2560  # 超大图只在加载时缩小一次，绘制时不再处理
_MUTED_TEXT_CONTRAST = 3.0  # 次要文字与文字底下背景的最低对比度（无背景图时各主题约 3.4~4.2）
_MASK_PAD = 12  # 文字区域外扩（逻辑像素），比模糊半径大，保证文字本身落在遮罩全实处
_MASK_SCALE = 4  # 遮罩按 1/4 分辨率绘制
_MASK_BLUR = 8  # 高斯模糊半径（逻辑像素）
_MASK_CLOSE = 5  # 闭运算窗口（1/8 分辨率像素，约 40 逻辑像素），填平外扩后剩下的窄缝
_MASK_BRIDGE = 240  # 同一行相邻文字间隔不超过这么多（逻辑像素）就连成一条，免得行尾长短不一留下暗色凹口
_MASK_REACH = _MASK_PAD + _MASK_CLOSE * 8 // 2 + _MASK_BLUR * 3  # 一个文字区域对遮罩的最大影响距离


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _encode(y: float) -> float:
    """线性亮度 → sRGB 编码值（0~1）。"""
    y = max(0.0, min(1.0, y))
    return 12.92 * y if y <= 0.0031308 else 1.055 * y ** (1 / 2.4) - 0.055


def relative_luminance(color: QColor) -> float:
    r, g, b = (_linear(c) for c in (color.redF(), color.greenF(), color.blueF()))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def luminance_luts() -> tuple[list[int], list[int], list[int]]:
    """三个通道各自的查表：sRGB 值 → 加权线性亮度 × 255（向下取整，三者相加略低于真实亮度，偏保守）。"""
    return tuple(
        [int(weight * _linear(v / 255) * 255) for v in range(256)]
        for weight in (0.2126, 0.7152, 0.0722)
    )


def lift_alpha_lut(bg: QColor, text_muted: QColor) -> list[int]:
    """按像素线性亮度（×255）查表得到往底色混合的比例（0~255）。
    目标亮度取"次要文字仍有 _MUTED_TEXT_CONTRAST 对比度"，不超过底色本身；够亮的像素比例为 0。
    混合在线性光空间进行，亮度按比例线性变化：解 a*Yb + (1-a)*Y = 目标。"""
    y_bg = relative_luminance(bg)
    y_floor = min(_MUTED_TEXT_CONTRAST * (relative_luminance(text_muted) + 0.05) - 0.05, y_bg)
    lut = []
    for i in range(256):
        y = i / 255
        if y >= y_floor:
            lut.append(0)
            continue
        lut.append(min(255, math.ceil((y_floor - y) / (y_bg - y) * 255)))  # 向上取整，宁可多提一点
    return lut


# sRGB 与线性光（×255）互转的查表，RGB 三通道共用
_TO_LINEAR = [round(_linear(v / 255) * 255) for v in range(256)] * 3
_FROM_LINEAR = [round(_encode(v / 255) * 255) for v in range(256)] * 3


def lift_image(solid: Image.Image, bg: QColor, text_muted: QColor) -> Image.Image:
    """RGB 图 → 提亮图：偏暗像素在线性光里往底色混合到刚好够用，够亮的像素原样保留。"""
    channels = [band.point(lut) for band, lut in zip(solid.split(), luminance_luts())]
    luma = ImageChops.add(ImageChops.add(channels[0], channels[1]), channels[2])
    mask = luma.point(lift_alpha_lut(bg, text_muted))
    # 转回 sRGB 后，不需要提亮的像素直接取原值，免得来回量化损失暗部细节
    bg_linear = Image.new("RGB", solid.size, (bg.red(), bg.green(), bg.blue())).point(_TO_LINEAR)
    mixed = Image.composite(bg_linear, solid.point(_TO_LINEAR), mask).point(_FROM_LINEAR)
    return Image.composite(mixed, solid, mask.point(lambda v: 255 if v else 0))


def _to_pixmap(image: Image.Image) -> QPixmap:
    data = image.tobytes("raw", "RGBA")
    qimage = QImage(data, image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())  # copy 后不再引用 data 的内存


def _load_images(path, bg: QColor, text_muted: QColor) -> tuple[QPixmap, QPixmap] | None:
    """返回 (原图, 提亮图)；读不了返回 None。"""
    try:
        with Image.open(path) as src:
            src.load()
            image = src.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if image.width > _MAX_WIDTH:
        image = image.resize((_MAX_WIDTH, round(image.height * _MAX_WIDTH / image.width)), Image.LANCZOS)
    bg_rgb = (bg.red(), bg.green(), bg.blue())
    # 透明区域本来就透出底色，先铺到底色上再算亮度
    solid = Image.alpha_composite(Image.new("RGBA", image.size, bg_rgb + (255,)), image).convert("RGB")
    lifted = lift_image(solid, bg, text_muted)
    return _to_pixmap(image), _to_pixmap(lifted.convert("RGBA"))


def _local_text_rects(widget: QWidget) -> list[QRect]:
    """控件里直接画在背景上的文字区域（控件坐标）；自带实色底的按钮等返回空。
    自绘文字的控件可实现 text_backdrop_rects() 自报区域。"""
    custom = getattr(widget, "text_backdrop_rects", None)
    if custom is not None:
        return [QRectF(r).toAlignedRect() for r in custom()]
    if isinstance(widget, QLabel):
        text = widget.text()
        if not text:
            return []
        box = widget.contentsRect()
        if "<" in text:  # 富文本算不准文字范围，整块算上
            return [box]
        flags = int(widget.alignment())
        if widget.wordWrap():  # 自动换行的标签往往横跨整行，按换行后的实际文字范围算
            return [widget.fontMetrics().boundingRect(box, flags | int(Qt.TextFlag.TextWordWrap), text)]
        return [widget.style().itemTextRect(widget.fontMetrics(), box, flags, widget.isEnabled(), text)]
    if isinstance(widget, (QCheckBox, QRadioButton, QComboBox, QLineEdit, QAbstractSpinBox)):
        return [widget.rect()]  # 输入框、下拉框是半透明白底，文字同样受背景影响
    if isinstance(widget, QPushButton) and (widget.isFlat() or widget.property("flat") in (True, "true")
                                            or widget.property("variant") == "secondary"):
        return [widget.rect()]  # 透明底按钮，文字也直接压在背景上
    return []


def _bridged(text_rects) -> list[tuple[int, int, int, int]]:
    """同一行（纵向重叠过半）里间隔不超过 _MASK_BRIDGE 的相邻文字之间补一块，连成整条。"""
    out = list(text_rects)
    ordered = sorted(text_rects)
    for i, (x, y, w, h) in enumerate(ordered):
        right = x + w
        for nx, ny, nw, nh in ordered[i + 1:]:
            if nx - right > _MASK_BRIDGE:
                break
            top, bottom = max(y, ny), min(y + h, ny + nh)
            if nx > right and bottom - top >= min(h, nh) / 2:
                out.append((right, top, nx - right, bottom - top))
                break  # 只连最近的一个，后面的由它继续往右连
    return out


def text_backdrop_rects(window: QWidget) -> tuple[tuple[int, int, int, int], ...]:
    """窗口内所有可见的、直接画在背景上的文字区域 (x, y, w, h)，窗口坐标，已裁掉被滚动区域遮住的部分。"""
    rects = []
    for widget in window.findChildren(QWidget):
        if not widget.isVisible() or widget.window() is not window:
            continue
        local = _local_text_rects(widget)
        if not local:
            continue
        visible = widget.visibleRegion().boundingRect()
        offset = widget.mapTo(window, QPoint(0, 0))
        for rect in local:
            rect = rect.intersected(visible)
            if not rect.isEmpty():
                rects.append((rect.x() + offset.x(), rect.y() + offset.y(), rect.width(), rect.height()))
    return tuple(rects)


class Background:
    def __init__(self):
        self.pixmap: QPixmap | None = None
        self.lifted: QPixmap | None = None
        self.opacity = 0.0
        self._source_key = None
        self._layers_key = None
        self._layers: tuple[QImage, QImage] | None = None
        # 主窗口和新建存档向导共用一个实例，合成结果按窗口分开缓存，免得交替重绘时互相顶掉
        self._frames: weakref.WeakKeyDictionary[QWidget, tuple] = weakref.WeakKeyDictionary()
        self.reload()

    def reload(self) -> None:
        path = get_custom_bg_path()
        self.opacity = float(get_custom_bg_opacity())
        if path is None:
            self.pixmap = self.lifted = None
            self._source_key = None
            self._drop_caches()
            return
        bg, muted = theme.color("BG_SOFT"), theme.color("TEXT_MUTED")
        stat = path.stat()
        # 字号变化等也会触发主题信号：图片和颜色都没变就不重新处理
        key = (str(path), stat.st_mtime_ns, stat.st_size, bg.rgb(), muted.rgb())
        if key == self._source_key:
            return
        images = _load_images(path, bg, muted)
        self.pixmap, self.lifted = images if images else (None, None)
        self._source_key = key
        self._drop_caches()

    def _drop_caches(self) -> None:
        self._layers_key = self._layers = None
        self._frames.clear()

    @property
    def active(self) -> bool:
        return self.pixmap is not None and self.opacity > 0

    def _blended_layers(self, width: int, height: int, dpr: float, smooth: bool) -> tuple[QImage, QImage]:
        """原图层和提亮图层：各自居中裁剪、缩放到窗口物理像素，按不透明度叠在底色上。
        只随尺寸、不透明度和底色变化，缓存下来，文字区域变化（滚动等）时只做遮罩合成。"""
        bg = theme.color("BG_SOFT")
        key = (width, height, dpr, smooth, self.opacity, bg.rgb())
        if key != self._layers_key:
            self._layers = tuple(self._blend(pixmap, bg, width, height, dpr, smooth)
                                 for pixmap in (self.pixmap, self.lifted))
            self._layers_key = key
        return self._layers

    def _blend(self, pixmap: QPixmap, bg: QColor, width: int, height: int, dpr: float, smooth: bool) -> QImage:
        sw, sh = pixmap.width(), pixmap.height()
        scale = max(width / sw, height / sh)
        cw, ch = width / scale, height / scale
        # 带 alpha 的格式：提亮图层之后要用遮罩裁 alpha，不带 alpha 会裁成黑色
        out = QImage(round(width * dpr), round(height * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        out.setDevicePixelRatio(dpr)
        out.fill(bg)
        painter = QPainter(out)
        painter.setOpacity(self.opacity)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        painter.drawPixmap(QRectF(0, 0, width, height), pixmap, QRectF((sw - cw) / 2, (sh - ch) / 2, cw, ch))
        painter.end()
        return out

    def paint(self, painter: QPainter, owner: QWidget, smooth: bool,
              text_rects: tuple[tuple[int, int, int, int], ...] = ()) -> QRegion:
        """在 owner 窗口已填好底色的画布上整窗绘制；text_rects 为直接画在背景上的文字区域 (x, y, w, h)，窗口坐标。
        返回因文字区域变化而需要补画的区域：遮罩的外扩、闭运算和模糊会波及控件外几十像素，
        而 Qt 只重绘变化的控件本身，调用方需对返回区域再 update() 一次，否则周围留下旧遮罩。"""
        if not self.active:
            return QRegion()
        width, height = owner.width(), owner.height()
        dpr = painter.device().devicePixelRatioF()
        key = (width, height, dpr, smooth, self.opacity, theme.color("BG_SOFT").rgb(), text_rects)
        old_key, frame = self._frames.get(owner, (None, None))
        stale = QRegion()
        if key != old_key:
            if old_key is not None and old_key[:-1] == key[:-1]:
                r, b = _MASK_REACH, _MASK_BRIDGE  # 横向还要算上与同行邻居之间的桥接
                for x, y, w, h in set(text_rects).symmetric_difference(old_key[-1]):
                    stale += QRect(x - r - b, y - r, w + 2 * (r + b), h + 2 * r)
            frame = self._compose(width, height, dpr, smooth, text_rects)
            self._frames[owner] = (key, frame)
        painter.drawImage(0, 0, frame)
        return stale

    def _compose(self, width, height, dpr, smooth, text_rects) -> QImage:
        raw, lifted = self._blended_layers(width, height, dpr, smooth)
        if not text_rects:
            return raw
        veil = lifted.copy()
        painter = QPainter(veil)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(QRectF(0, 0, width, height), self._mask(width, height, text_rects))
        painter.end()
        frame = raw.copy()
        painter = QPainter(frame)
        painter.drawImage(0, 0, veil)
        painter.end()
        return frame

    @staticmethod
    def _mask(width: int, height: int, text_rects) -> QImage:
        """低分辨率遮罩：文字区域外扩成圆角块再高斯模糊，放大到窗口时自然羽化，相邻文字的提亮区融成一片。"""
        s = _MASK_SCALE
        mask = QImage(max(1, width // s), max(1, height // s), QImage.Format.Format_Grayscale8)
        mask.fill(0)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        radius = _MASK_PAD / s
        for x, y, w, h in _bridged(text_rects):
            painter.drawRoundedRect(QRectF((x - _MASK_PAD) / s, (y - _MASK_PAD) / s,
                                           (w + 2 * _MASK_PAD) / s, (h + 2 * _MASK_PAD) / s), radius, radius)
        painter.end()
        solid = Image.frombuffer("L", (mask.width(), mask.height()), bytes(mask.constBits()), "raw", "L",
                                 mask.bytesPerLine(), 1)
        # 闭运算填掉同一行相邻文字之间的窄缝，免得提亮区碎成一块块；在再缩小一半的图上做，开销只有几毫秒
        half = solid.resize((max(1, solid.width // 2), max(1, solid.height // 2)), Image.BOX)
        closed = half.filter(ImageFilter.MaxFilter(_MASK_CLOSE)).filter(ImageFilter.MinFilter(_MASK_CLOSE))
        solid = ImageChops.lighter(solid, closed.resize(solid.size, Image.BILINEAR))
        blurred = solid.filter(ImageFilter.GaussianBlur(_MASK_BLUR / s))
        out = QImage(blurred.tobytes(), blurred.width, blurred.height, blurred.width, QImage.Format.Format_Alpha8)
        return out.copy()  # copy 后不再引用 tobytes 的临时内存
