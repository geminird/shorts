"""无头验证：线宽与文字字号解耦;线宽加细到 1px。

验证：
  1. 线宽档 [1,2,4]、默认 2;width_buttons 键 {1,2,4};
  2. 文字字号独立 current_text_size(默认 14)、档 {12,16,22};
  3. text 工具:width_group 隐藏、text_size_group 显示;矩形反之;
  4. _select_text_size/_select_width 设值与高亮;
  5. 解耦关键:线宽=1 时文字字号仍为 current_text_size(不是线宽×3=3);
  6. 矩形边框随线宽变细(thickness 1 < 4);
  7. 老文字标注(width 存字号)绘制兼容。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_width_text_decouple.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QRect, QPoint  # noqa: E402
from PyQt6.QtGui import QPixmap, QPainter, QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


# 清理 QSettings,避免被前序运行持久化的 width/text_size/tool 污染默认值断言
M.QSettings("Shorts", "Shorts").clear()
win = M.SelectionWindow()
win.show()
win._create_toolbar()
win._create_style_panel()  # width_group/text_size_group 已移入 style_panel

# 1. 线宽档与默认
check("线宽档 {1,2,4}", set(win.width_buttons) == {1, 2, 4}, str(set(win.width_buttons)))
check("默认线宽 2", win.current_width == 2, str(win.current_width))

# 2. 字号独立
check("字号档 {12,16,22}", set(win.text_size_buttons) == {12, 16, 22},
      str(set(win.text_size_buttons)))
check("默认字号 14", win.current_text_size == 14, str(win.current_text_size))

# 3. 显隐切换(用 isHidden;父窗口已 show 但 isVisible 对未映射子部件不可靠)
win._select_tool("text")
check("text 工具: 线宽组隐藏", win.width_group.isHidden() is True)
check("text 工具: 字号组显示", win.text_size_group.isHidden() is False)
win._select_tool("rectangle")
check("矩形工具: 线宽组显示", win.width_group.isHidden() is False)
check("矩形工具: 字号组隐藏", win.text_size_group.isHidden() is True)

# 4. 选择与高亮
win._select_width(1)
check("选线宽1: current_width=1", win.current_width == 1)
check("选线宽1: 按钮1 active", win.width_buttons[1].property("active") is True)
win._select_text_size(22)
check("选字号22: current_text_size=22", win.current_text_size == 22)
check("选字号22: 按钮22 active", win.text_size_buttons[22].property("active") is True)

# 5. 解耦关键:线宽=1 时文字字号仍为独立值(不是 1×3=3)
win.current_width = 1
win._select_text_size(16)
win._handle_text_annotation(QPoint(50, 50))   # 创建文字输入框
ps = win.text_input.font().pointSize() if win.text_input else None
check("线宽1时文字输入框字号=16(独立,非3)", ps == 16, f"pointSize={ps}")
if win.text_input:
    win.text_input.setText("测字")
    win._finish_text_input()
    ann = win.annotations[-1] if win.annotations else {}
    check("提交文字标注 width=字号16(非线宽×3=3)", ann.get("width") == 16,
          f"width={ann.get('width')}")


# 6. 矩形边框随线宽变细
def left_border_thickness(w):
    pm = QPixmap(60, 40)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    ann = {"tool": "rectangle", "rect": QRect(10, 10, 40, 20),
           "color": QColor(255, 0, 0), "width": w}
    M.SelectionWindow()._draw_annotation(p, ann, 0, 0)
    p.end()
    img = pm.toImage()
    n = 0
    for x in range(10, 60):  # y=15 行,从左边框起数连续不透明像素
        if img.pixelColor(x, 15).alpha() > 40:
            n += 1
        else:
            break
    return n


t1 = left_border_thickness(1)
t4 = left_border_thickness(4)
check("矩形边框随线宽变细(1<4)", t1 < t4, f"t1={t1} t4={t4}")
check("线宽1边框足够细(≤2px)", t1 <= 2, f"t1={t1}")

# 7. 老文字标注(width 存字号)绘制兼容——不崩且渲染出文字
pm = QPixmap(120, 40)
pm.fill(Qt.GlobalColor.transparent)
p = QPainter(pm)
old_text_ann = {"tool": "text", "rect": QRect(5, 5, 100, 30),
                "color": QColor(0, 0, 0), "width": 18, "text": "兼容"}
win._draw_annotation(p, old_text_ann, 0, 0)
p.end()
opaque = sum(1 for y in range(pm.height()) for x in range(pm.width())
             if pm.toImage().pixelColor(x, y).alpha() > 40)
check("老文字标注(width=18)绘制兼容(有像素)", opaque > 0, f"opaque={opaque}")

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
