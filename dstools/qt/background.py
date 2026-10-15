"""自定义背景图：主窗口 paintEvent 整窗只画一次（底色 + 居中裁剪填满的图片按不透明度叠加），子控件透明透出。

所有主题都是浅底深字，图里偏暗的区域在高不透明度下会吃掉文字对比度。加载时先做一次"保字"处理：
比底色 BG_SOFT 暗的像素，亮度按比例压进 [下限, 底色亮度]（往底色混合），明暗层次保留、只是变浅；
比底色亮的像素原样保留。下限取"次要文字色 TEXT_MUTED 在其上仍有 2.5:1 对比度"（主文字此时约 5:1 以上）。
不透明度叠加只会在 BG_SOFT 与处理后的图之间插值，因此拉满也不会低于下限。
换主题时底色和文字色变了，需要 reload()。

缩放过程中用快速变换，停顿后再用平滑变换重画。
"""

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from dstools.qt.theme import theme
from dstools.shared.custom_background import get_custom_bg_path
from dstools.shared.app_settings import get_custom_bg_opacity

_MAX_WIDTH = 2560  # 超大图只在加载时缩小一次，绘制时不再处理
_MUTED_TEXT_CONTRAST = 2.5  # 次要文字与最暗背景的最低对比度


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(color: QColor) -> float:
    r, g, b = (_linear(c) for c in (color.redF(), color.greenF(), color.blueF()))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _gray_for_luminance(y: float) -> int:
    """相对亮度 y 对应的 sRGB 灰度值（0~255）。"""
    v = 12.92 * y if y <= 0.0031308 else 1.055 * y ** (1 / 2.4) - 0.055
    return round(max(0.0, min(1.0, v)) * 255)


def luma_floor(text_muted: QColor, bg_luma: int) -> int:
    """背景亮度下限（灰度 0~255）：保证次要文字对比度，且不超过主题底色本身。"""
    y = _MUTED_TEXT_CONTRAST * (_relative_luminance(text_muted) + 0.05) - 0.05
    return min(_gray_for_luminance(y), bg_luma)


def lift_mask_lut(floor: int, bg_luma: int) -> list[int]:
    """按像素亮度查表得到往底色混合的比例（0~255）。
    目标亮度 floor + (bg - floor) * L / bg 是线性压缩，解 k*bg + (1-k)*L = 目标 得 k = floor / bg（与 L 无关），
    比底色亮的像素 k = 0；两段在 L = bg 处输出亮度连续。"""
    k = round(255 * floor / bg_luma) if bg_luma > 0 else 0
    return [k if luma < bg_luma else 0 for luma in range(256)]


def _readable_image(path, bg: QColor, text_muted: QColor) -> QPixmap | None:
    try:
        with Image.open(path) as src:
            src.load()
            image = src
            if image.width > _MAX_WIDTH:
                image = image.resize((_MAX_WIDTH, round(image.height * _MAX_WIDTH / image.width)), Image.LANCZOS)
            bg_rgb = (bg.red(), bg.green(), bg.blue())
            # 透明区域本来就透出底色，先铺到底色上再处理
            image = image.convert("RGBA")
            base = Image.new("RGBA", image.size, bg_rgb + (255,))
            image = Image.alpha_composite(base, image).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    bg_luma = Image.new("RGB", (1, 1), bg_rgb).convert("L").getpixel((0, 0))
    mask = image.convert("L").point(lift_mask_lut(luma_floor(text_muted, bg_luma), bg_luma))
    image = Image.composite(Image.new("RGB", image.size, bg_rgb), image, mask)
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())  # copy 后不再引用 data 的内存


class Background:
    def __init__(self):
        self.pixmap: QPixmap | None = None
        self.opacity = 0.0
        self._source_key = None
        self.reload()

    def reload(self) -> None:
        path = get_custom_bg_path()
        self.opacity = float(get_custom_bg_opacity())
        if path is None:
            self.pixmap, self._source_key = None, None
            return
        bg, muted = theme.color("BG_SOFT"), theme.color("TEXT_MUTED")
        stat = path.stat()
        # 字号变化等也会触发主题信号：图片和颜色都没变就不重新处理
        key = (str(path), stat.st_mtime_ns, stat.st_size, bg.rgb(), muted.rgb())
        if key == self._source_key:
            return
        self.pixmap = _readable_image(path, bg, muted)
        self._source_key = key

    @property
    def active(self) -> bool:
        return self.pixmap is not None and self.opacity > 0

    def paint(self, painter: QPainter, width: int, height: int, smooth: bool) -> None:
        if not self.active:
            return
        sw, sh = self.pixmap.width(), self.pixmap.height()
        scale = max(width / sw, height / sh)
        cw, ch = width / scale, height / scale
        painter.save()
        painter.setOpacity(self.opacity)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        painter.drawPixmap(
            QRectF(0, 0, width, height), self.pixmap,
            QRectF((sw - cw) / 2, (sh - ch) / 2, cw, ch),
        )
        painter.restore()
