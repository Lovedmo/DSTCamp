"""主题窗口点缀：贴在主窗口外框上、伸出窗外的小图（精灵），每张由一个跟随主窗口的透明小窗承载；
另有一道画在主窗口自身外框上、从左上往右下渐隐的主题色描边。

构图讲主次，各主题共用一个骨架：左上角一簇作主角，沿上边、左边各延伸一串，末端越飘越稀、越淡；
右上角一组作呼应，右下角一组收尾，左下角留白。樱花主题右下角是窗内散落的花朵花瓣；
篝火主题右下角是跨在拐角上的一堆篝火，火星沿右边框往上飘。
图片素材在 icons/ornaments/（来源与授权见同目录 *_SOURCES.txt），按 3 倍屏预渲染成 PNG；
精灵按 (主题, 序号, 缩放比) 缩放到物理像素并缓存，主窗口移动时只挪小窗位置，不重画。
主窗口本身不开 WA_TranslucentBackground（整窗变分层窗口会拖慢绘制和缩放），只有这些小窗是透明的。
"""

import math
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from dstools.qt.theme import theme
from dstools.shared.resource_paths import bundled_resource_dir

_ASSET_DIR = bundled_resource_dir() / "icons" / "ornaments"


@dataclass(frozen=True)
class Sprite:
    image: str  # icons/ornaments/ 下的文件名
    fx: float  # 锚点在主窗口上的相对位置：0 左/上缘，1 右/下缘
    fy: float
    x: float  # 图片中心相对锚点的偏移（逻辑像素，负数即往左/上伸出窗外）
    y: float
    height: float  # 显示高度（逻辑像素），宽度按原图比例
    rotation: float = 0.0
    opacity: float = 1.0


# ── 注册表 ───────────────────────────────────────────────────────────
# 标题栏高 32：图标约在 x 10~28、y 7~25，标题文字从 x 约 34 起。主花团允许压住标题开头。
# 各张图的相对位置在 reference/dev_scripts/ornaments/ 的排版脚本里一起排好（樱花 compose.py、
# 篝火 compose_campfire.py），那边重新导出图片后按它打印的坐标同步这里。

_SPRITES: dict[str, tuple[Sprite, ...]] = {
    "campfire": (
        # 从左上角贴着上边框向右、贴着左边框向下各一根挂满秋叶的细枝，末端收到外框线上，落叶渐稀渐淡
        Sprite("campfire_spray_right.png", 0, 0, 307, -9.5, 61),
        Sprite("campfire_spray_down.png", 0, 0, -11, 242, 500),
        # 左上主角：一簇大叶压在拐角上，后创建的小窗叠在细枝上面
        Sprite("campfire_cluster.png", 0, 0, 8, 1.5, 93),
        # 右上呼应：拐角一片大枫叶，沿上边往左、沿右边往下各一段挂叶细枝
        Sprite("campfire_spray_corner.png", 1, 0, -153.5, 94.5, 253),
        # 右下收尾：拐角铺一圈落叶，篝火跨在拐角上、底下一层暖光，火星贴着右边框往上飘；左下角留白
        Sprite("campfire_fire.png", 1, 1, -147, -150.5, 337),
    ),
    "sakura": (
        # 从左上角沿上边框向右、沿左边框向下各一串花朵、花蕾，粉色细茎相连，末端收向外框线、花瓣渐稀渐淡
        Sprite("sakura_spray_right.png", 0, 0, 282.5, -14.5, 63),
        Sprite("sakura_spray_down.png", 0, 0, -18, 218, 458),
        # 左上主角：花团压在拐角上、略往窗内，后创建的小窗叠在花串上面
        Sprite("sakura_cluster.png", 0, 0, 12, -2, 92),
        # 右上呼应：拐角一朵大花，沿上边往左、沿右边往下各一串
        Sprite("sakura_spray_corner.png", 1, 0, -228.5, 132, 322),
        # 右下角：拐角几朵大花，花朵花瓣沿下边缘向左散落、越往左越贴边越稀，右边花朵上方也飘几片；左下角留白
        Sprite("sakura_spray_bottom.png", 1, 1, -180.5, -84.5, 185),
    ),
    "twilight": (
        # 排版脚本 compose_twilight.py。沿上边框向右、沿左边框向下各一串金线挂星，往外鼓出，
        # 串边撒一道星屑，越远越小越稀越淡；星只停在外框线外侧，不挡标题文字
        Sprite("twilight_spray_right.png", 0, 0, 304.5, -15, 34),
        Sprite("twilight_spray_down.png", 0, 0, -19, 257.5, 429),
        # 左上主角：一弯月牙斜靠在拐角外侧，下方两朵小云托着，周围几颗星
        Sprite("twilight_cluster.png", 0, 0, -2, -17.5, 123),
        # 右上呼应：拐角一颗大星，沿上边往左、沿右边往下各一小串挂星；都不挡标题栏按钮
        Sprite("twilight_spray_corner.png", 1, 0, -163.5, 125.5, 311),
        # 右下角：两朵云压在拐角，星星沿下边缘往左、沿右边缘往上散开；左下角留白
        Sprite("twilight_bottom.png", 1, 1, -185, -124, 258),
    ),
}

_images: dict[str, QImage] = {}
_cache: dict[tuple[str, int, float], QPixmap] = {}


def _image(name: str) -> QImage:
    if name not in _images:
        _images[name] = QImage(str(_ASSET_DIR / name))
    return _images[name]


def _logical_size(sprite: Sprite) -> tuple[float, float, float, float]:
    """(原图显示宽, 显示高, 旋转后外接框宽, 外接框高)，均为逻辑像素。"""
    image = _image(sprite.image)
    h = sprite.height
    w = h * image.width() / max(1, image.height())
    angle = math.radians(sprite.rotation)
    cos, sin = abs(math.cos(angle)), abs(math.sin(angle))
    return w, h, w * cos + h * sin, w * sin + h * cos


def _rendered(theme_name: str, index: int, sprite: Sprite, dpr: float) -> QPixmap:
    """按缩放比把原图缩到物理像素、旋转后画进透明 QPixmap。"""
    key = (theme_name, index, dpr)
    if key not in _cache:
        w, h, box_w, box_h = _logical_size(sprite)
        pixmap = QPixmap(math.ceil(box_w * dpr), math.ceil(box_h * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        scaled = _image(sprite.image).scaled(
            round(w * dpr), round(h * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(sprite.opacity)
        painter.translate(box_w / 2, box_h / 2)
        painter.rotate(sprite.rotation)
        painter.drawImage(QPointF(-w / 2, -h / 2), scaled)
        painter.end()
        _cache[key] = pixmap
    return _cache[key]


def paint_frame_accent(painter: QPainter, rect: QRectF) -> None:
    """在主窗口外框上叠一道主题色描边：沿对角线从左上角最浓渐隐到右下角。
    16:9 的窗口里，左边整条、上边大半可见，右边和下边后段基本看不到。"""
    strong = QColor(theme.color("ORNAMENT_ACCENT"))
    strong.setAlpha(220)
    middle = QColor(theme.color("ORNAMENT"))
    middle.setAlpha(170)
    faded = QColor(theme.color("ORNAMENT"))
    faded.setAlpha(0)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, strong)
    gradient.setColorAt(0.35, middle)
    gradient.setColorAt(0.75, faded)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(gradient, 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect)
    painter.restore()


class _SpriteWindow(QWidget):
    """承载一张精灵的透明小窗：以 Tool 窗口归主窗口所有，始终叠在主窗口之上、
    不进任务栏和 Alt+Tab，被其他程序遮挡时一起被挡；鼠标穿透。"""

    def __init__(self, owner: QWidget):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.index = -1

    def paintEvent(self, _event) -> None:
        sprites = _SPRITES.get(theme.name, ())
        if not 0 <= self.index < len(sprites):
            return
        painter = QPainter(self)
        painter.drawPixmap(QPointF(0, 0), _rendered(theme.name, self.index, sprites[self.index],
                                                    self.devicePixelRatioF()))


class WindowOrnaments(QObject):
    """管理主窗口四周的点缀小窗：跟随主窗口移动和缩放；主窗口隐藏、最小化或伪最大化
    （贴满工作区，伸出部分会跑到屏幕外或相邻显示器上）时全部隐藏。"""

    def __init__(self, window: QWidget, is_maximized: Callable[[], bool], enabled: bool):
        super().__init__(window)
        self._window = window
        self._is_maximized = is_maximized
        self._enabled = enabled
        self._windows: list[_SpriteWindow] = []
        window.installEventFilter(self)
        theme.changed.connect(self._on_theme_changed)

    def frame_accent_visible(self) -> bool:
        """主窗口外框是否叠画主题色渐隐描边（伪最大化时也保留，它画在窗口内部）。"""
        return self._enabled and theme.name in _SPRITES

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.refresh()
        self._window.update()

    def refresh(self) -> None:
        """按主窗口当前位置和状态摆放或隐藏全部小窗。"""
        sprites = _SPRITES.get(theme.name, ())
        window = self._window
        visible = (self._enabled and window.isVisible() and not window.isMinimized()
                   and not self._is_maximized())
        while len(self._windows) < len(sprites):
            self._windows.append(_SpriteWindow(window))
        for i, sprite_window in enumerate(self._windows):
            if not visible or i >= len(sprites):
                sprite_window.hide()
                continue
            sprite = sprites[i]
            _, _, box_w, box_h = _logical_size(sprite)
            sprite_window.index = i
            sprite_window.setGeometry(
                round(window.x() + window.width() * sprite.fx + sprite.x - box_w / 2),
                round(window.y() + window.height() * sprite.fy + sprite.y - box_h / 2),
                math.ceil(box_w), math.ceil(box_h),
            )
            sprite_window.show()

    def _on_theme_changed(self) -> None:
        self.refresh()
        for sprite_window in self._windows:
            sprite_window.update()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._window and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide,
            QEvent.Type.WindowStateChange,
        ):
            self.refresh()
        return False
