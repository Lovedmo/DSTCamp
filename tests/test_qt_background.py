"""自定义背景图"保字"处理：任何主题下，背景图拉满不透明度后最暗处仍保证次要文字对比度。"""

import _harness  # noqa: F401  # 把项目根目录加入 sys.path
from PIL import Image
from PySide6.QtGui import QColor

from dstools.qt.background import (
    _MUTED_TEXT_CONTRAST, _relative_luminance, lift_mask_lut, luma_floor,
)
from dstools.shared.palettes import THEMES


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_darkest_pixel_keeps_muted_text_readable_in_every_theme():
    for name, palette in THEMES.items():
        bg, muted = QColor(palette["BG_SOFT"]), QColor(palette["TEXT_MUTED"])
        bg_rgb = (bg.red(), bg.green(), bg.blue())
        bg_luma = Image.new("RGB", (1, 1), bg_rgb).convert("L").getpixel((0, 0))
        k = lift_mask_lut(luma_floor(muted, bg_luma), bg_luma)[0] / 255
        # 纯黑像素按同样比例往底色混合后的颜色
        lifted = QColor(*(round(c * k) for c in bg_rgb))
        # 允许 0.1 的取整误差
        assert _contrast(muted, lifted) >= _MUTED_TEXT_CONTRAST - 0.1, name


def test_pixels_brighter_than_theme_bg_are_untouched():
    lut = lift_mask_lut(180, 240)
    assert lut[239] > 0 and lut[240] == 0 and lut[255] == 0


_harness.run(globals())
