"""自定义背景图"保字"：任何主题下，提亮图的每个像素与次要文字都保持最低对比度。"""

import _harness  # noqa: F401  # 把项目根目录加入 sys.path
from PIL import Image
from PySide6.QtGui import QColor

from dstools.qt.background import _MUTED_TEXT_CONTRAST, lift_image, relative_luminance
from dstools.shared.palettes import THEMES


def test_lifted_pixels_keep_muted_text_readable_in_every_theme():
    # 饱和色在 sRGB 编码空间混合会掉亮度，所以覆盖整个色立方而不只是灰阶
    values = range(0, 256, 15)
    colors = [(r, g, b) for r in values for g in values for b in values]
    source = Image.new("RGB", (len(colors), 1))
    source.putdata(colors)
    for name, palette in THEMES.items():
        bg, muted = QColor(palette["BG_SOFT"]), QColor(palette["TEXT_MUTED"])
        y_muted = relative_luminance(muted)
        for color, lifted in zip(colors, lift_image(source, bg, muted).getdata()):
            contrast = (relative_luminance(QColor(*lifted)) + 0.05) / (y_muted + 0.05)
            # 允许 0.05 的 8 位量化误差
            assert contrast >= _MUTED_TEXT_CONTRAST - 0.05, (name, color, lifted, contrast)


_harness.run(globals())
