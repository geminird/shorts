"""无头验证：箭头头部尺寸已调小(倍数 14×→6×)。

验证：
  1. 水平箭头头部翼展(垂直最大跨度) ≈ 6*w：w=4→~24px，远小于改前 14*w(~56px)；
  2. 翼展随线宽递增(2<4<6)；
  3. w=4 整体不透明像素数显著低于改前量级(改前 ~3500+ → 改后 ~1300)。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_arrow_size.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QPoint  # noqa: E402
from PyQt6.QtGui import QPixmap, QPainter, QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
win = M.SelectionWindow()

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


def render_arrow(w, length=200):
    """画一条水平箭头;返回 (head_span_px, opaque_count)。
    head_span = 所有列中不透明像素的垂直跨度最大值(即头部翼展)。"""
    pm = QPixmap(length + 40, 90)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    start = QPoint(20, 45)
    end = QPoint(20 + length, 45)
    win._draw_arrow_head(p, start, end, w, QColor(255, 59, 48))
    p.end()
    img = pm.toImage()
    span_max = 0
    opaque = 0
    for x in range(img.width()):
        ys = []
        for y in range(img.height()):
            if img.pixelColor(x, y).alpha() > 40:
                ys.append(y)
                opaque += 1
        if ys:
            span_max = max(span_max, max(ys) - min(ys))
    return span_max, opaque


# 头部翼展应 ≈ 6*w(受 length*0.35 上限与抗锯齿影响,给容差)
spans = {}
for w, lo, hi in [(2, 8, 16), (4, 18, 30), (6, 28, 42)]:
    span, _ = render_arrow(w)
    spans[w] = span
    check(f"w={w} 头部翼展 {span}px 在 [{lo},{hi}]（改前约 {14 * w}px）",
          lo <= span <= hi, f"span={span}")

# 翼展随线宽递增
check("翼展随线宽递增 2<4<6",
      spans[2] < spans[4] < spans[6], str(spans))

# w=4 总像素数应远低于改前量级(改前 ~3500+)
_, op4 = render_arrow(4)
check("w=4 不透明像素数显著低于改前(<2200)", op4 < 2200, f"opaque={op4}")

# 线宽已加细档(1/2/4)、与文字字号解耦(详见 _test_width_text_decouple)
_w = M.SelectionWindow()
_w._create_toolbar()
_w._create_style_panel()  # width_buttons 已移入 style_panel
check("线宽档 {1,2,4}(已加细,默认2)", set(_w.width_buttons) == {1, 2, 4},
      str(set(_w.width_buttons)))

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
