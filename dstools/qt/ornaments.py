"""主题窗口点缀：贴在主窗口外框上、伸出窗外的小图（精灵），每张由一个跟随主窗口的透明小窗承载；
另有一道画在主窗口自身外框上、从左上往右下渐隐的主题色描边。

构图讲主次，各主题共用一个骨架：左上角一簇作主角，沿上边、左边各延伸一串，末端越飘越稀、越淡；
右上角一组作呼应，右下角一组收尾，左下角留白。樱花主题右下角是窗内散落的花朵花瓣；
篝火主题右下角是跨在拐角上的一堆篝火，火星沿右边框往上飘；星夜蓝右下角是跨在拐角上的松林和林前帐篷，萤火虫往外飞散。
图片素材在 icons/ornaments/（来源与授权见同目录 *_SOURCES.txt），按 3 倍屏预渲染成 PNG；
精灵按 (主题, 序号, 缩放比) 缩放到物理像素并缓存，主窗口移动时只挪小窗位置，不重画。
主窗口本身不开 WA_TranslucentBackground（整窗变分层窗口会拖慢绘制和缩放），只有这些小窗是透明的。
"""

import math
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt
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
# 篝火 compose_campfire.py、自然绿 mint/compose_mint.py、天空 sky/compose_sky.py、星夜 twilight/compose_twilight.py），那边重新导出图片后按它打印的坐标同步这里。

_SPRITES: dict[str, tuple[Sprite, ...]] = {
    "campfire": (
        # 从左上角贴着上边框向右、贴着左边框向下各一根挂满秋叶的细枝，末端收到外框线上，落叶渐稀渐淡
        Sprite("campfire_spray_right.png", 0, 0, 310.5, -16.5, 63),
        Sprite("campfire_spray_down.png", 0, 0, -10.5, 242, 500),
        # 左上主角：一簇大叶压在拐角上，后创建的小窗叠在细枝上面
        Sprite("campfire_cluster.png", 0, 0, 8, 1.5, 93),
        # 右上呼应：拐角一片大枫叶，沿上边往左、沿右边往下各一段挂叶细枝
        Sprite("campfire_spray_corner.png", 1, 0, -155.5, 91.5, 259),
        # 右下收尾：拐角铺一圈落叶，篝火收在拐角里侧、两旁垫几片大叶、底下一层暖光，火星飘到右边框再往上飘；左下角留白
        Sprite("campfire_fire.png", 1, 1, -151.5, -155, 328),
    ),
    "mint": (
        # 常春藤：一根从左上角鸟窝底下的草丛里沿左边垂下来（先创建，压在鸟窝、蕨叶、绿枝下面，和草叶交织成一片）
        Sprite("mint_ivy_strand.png", 0, 0, -45.5, 138, 300),
        # 自然绿：从左上角沿上边框向右、沿左边框向下各一条枝叶带，后面是叶子朝外的绿枝，中间探出蕨叶，
        # 最前面是从框线上长出来的一片草（前后两排加一层贴边嫩草），草根扎进框线、脚下垫苔藓晕影；
        # 上边从鸟窝往右、右上角往左都是起点最高，平滑变矮、变稀、变淡，到中间自然消失
        Sprite("mint_spray_right.png", 0, 0, 228, -26, 70),
        Sprite("mint_spray_down.png", 0, 0, -16.5, 130.5, 273),
        # 左上主角：拐角上方一个鸟窝半埋在草丛里，绿绣眼坐在窝里，几片蕨叶从拐角放射；后创建的小窗叠在两条带上面
        Sprite("mint_cluster.png", 0, 0, 2, -21.5, 101),
        # 右上呼应：沿上边往左、沿右边往下各一条短些的枝叶带，拐角两片蕨叶，一根短常春藤沿右边垂下
        Sprite("mint_spray_corner.png", 1, 0, -68.5, 65, 256),
        # 右下角：小草从拐角长起，沿下边缘往左越来越矮、越稀、越淡直到消失，蕨叶贴着右边往上伸，三叶草藏在草里
        Sprite("mint_spray_bottom.png", 1, 1, -196.5, -50.5, 101),
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
    "sky": (
        # 排版脚本 sky/compose_sky.py，写实静谧的高空风格，云朵按体积生成、形状各不相同。从左上角沿上边框
        # 向右、沿左边框向下各一串积云，总体越远越小越淡，间距疏密不一、有的两朵叠在一起，高低错开（左边
        # 一簇、一对、一朵成簇分布，离框远近交错、有的压住边框，不排成一列），间几缕薄雾、零星飞鸟，上边末端
        # 一段雁阵；上边一串只铺到四百左右，不占满半个窗口，圆鼓的小积云和扁平的碎积云混排
        Sprite("sky_spray_right.png", 0, 0, 259, -31.5, 63),
        Sprite("sky_spray_down.png", 0, 0, -35.5, 250, 312),
        # 左上主角：一朵最大的扁团状积云跨在拐角上往窗内收，右上方后面叠一片小些的积云，左下方错开一朵形状
        # 不同的大积云（只交叠一角），底下沿左边框外垂下一道雨帘（断续的雨丝垂直落下、带亮点、末端参差）；
        # 压住标题栏图标和标题开头几个字母、不挡菜单，背后透出柔和日光，一缕薄雾横过拐角，几只海鸥飞过
        Sprite("sky_cluster.png", 0, 0, 23.5, 51, 270),
        # 右上呼应：风筝停在拐角外框线上方，风筝线沿上边往左淡去，几只鸟绕着飞；框外上方一串疏密不一的云，
        # 菜单栏右端跨在右边框上的一大一小两朵与框外连成一片，右边框上再往下散落几朵、压住边框，
        # 其中大些的一朵底下也垂一道雨帘；都不挡标题栏按钮
        Sprite("sky_spray_corner.png", 1, 0, -123, 118, 372),
        # 右下角：一道低矮的积云带压在拐角，几朵小积云沿下边框线往左散开，往左延一条薄层云，几只鸟沿右边框
        # 外侧往上飞、几只沿下边框外侧飞；左下角留白
        Sprite("sky_bottom.png", 1, 1, -140.5, -73, 190),
    ),
    "twilight": (
        # 星夜蓝，排版脚本 twilight/compose_twilight.py。沿上边框向右、沿左边框向下各一道银河光带，
        # 越远越淡，光带里撒满小星，北斗七星、仙后座（按真实星位）叠在上面；左边光带紧贴外框线
        Sprite("twilight_spray_right.png", 0, 0, 396, -30, 84),
        Sprite("twilight_spray_down.png", 0, 0, -29, 318.5, 533),
        # 左上主角：一轮真实月面（NASA 月面贴图渲染）的盈凸月带月晕，前后两团月光镶边的夜云，
        # 周围几道小流星；压住标题开头
        Sprite("twilight_cluster.png", 0, 0, -6, -24.5, 219),
        # 右上呼应：几道流星收在拐角附近、两道斜切进窗口落在按钮下方；不挡标题栏按钮
        Sprite("twilight_spray_corner.png", 1, 0, -36, 14.5, 185),
        # 右下角：松林跨在拐角上，帐篷搭在林前、门里透出暖光，萤火虫在林边飞散；左下角留白
        Sprite("twilight_bottom.png", 1, 1, -116, -116, 242),
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

    def follow_native_rect(self, left: int, top: int, right: int, bottom: int) -> None:
        """系统拖动或缩放主窗口时（WM_MOVING / WM_SIZING），按即将生效的窗口矩形（物理像素）先把小窗摆好，
        和主窗口在同一帧里到位；等主窗口真的动了再跟，小窗会慢一拍、和边框之间闪出缝隙。"""
        screen = self._window.screen()
        if screen is None:
            return
        # Qt 以各屏幕左上角为原点缩放：逻辑坐标 = 原点 + (物理坐标 - 原点) / 缩放比
        dpr = screen.devicePixelRatio()
        origin = screen.geometry().topLeft()
        self.refresh(QRect(round(origin.x() + (left - origin.x()) / dpr), round(origin.y() + (top - origin.y()) / dpr),
                           round((right - left) / dpr), round((bottom - top) / dpr)))

    def refresh(self, geometry: QRect | None = None) -> None:
        """按主窗口位置（缺省取当前位置）和状态摆放或隐藏全部小窗。"""
        sprites = _SPRITES.get(theme.name, ())
        window = self._window
        if geometry is None:
            # 拖上边框缩放时 Qt 先发移动事件、再发缩放事件，控件的 geometry 在两者之间是"新位置 + 旧尺寸"，
            # 右下角的小窗会被摆错一下；原生窗口的 geometry 位置和尺寸是一起更新的
            handle = window.windowHandle()
            geometry = handle.geometry() if handle is not None else window.geometry()
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
                round(geometry.x() + geometry.width() * sprite.fx + sprite.x - box_w / 2),
                round(geometry.y() + geometry.height() * sprite.fy + sprite.y - box_h / 2),
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
