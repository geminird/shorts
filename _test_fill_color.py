"""无头验证：矩形/椭圆的边框色 + 填充色分设。

验证：
  1. 工具栏构建含 fill_group;默认隐藏;矩形/椭圆时显示,箭头时隐藏;
  2. current_fill_color 默认 None;_select_fill_color 正确设置并高亮(蓝框);
  3. 无填充(显式 None / 老标注缺键)矩形/椭圆内部透明(alpha=0);
  4. 有填充矩形/椭圆内部为半透明填充色(alpha>100 且偏该色)。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_fill_color.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QRect  # noqa: E402
from PyQt6.QtGui import QPixmap, QPainter, QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


win = M.SelectionWindow()
win._create_toolbar()
win._create_style_panel()  # 样式组(fill_group 等)已移入 style_panel
check("工具栏构建含 fill_group", hasattr(win, "fill_group"))
win._select_tool("arrow")  # 确保非矩形/椭圆,fill_group 应隐藏(默认工具可能被前序测试持久化)
check("arrow 工具: fill_group 隐藏", win.fill_group.isHidden() is True)
check("current_fill_color 默认 None", win.current_fill_color is None)

# 切矩形/椭圆显示填充组,切箭头隐藏
# (用 isHidden 判断显隐意图;父窗口未 show 时 isVisible() 不可靠)
win._select_tool("rectangle")
check("矩形工具: fill_group 显示", win.fill_group.isHidden() is False)
win._select_tool("ellipse")
check("椭圆工具: fill_group 显示", win.fill_group.isHidden() is False)
win._select_tool("arrow")
check("箭头工具: fill_group 隐藏", win.fill_group.isHidden() is True)

# 选择填充色与高亮
win._select_tool("rectangle")
win._select_fill_color(QColor("#007aff"), "#007aff")
check("选填充色蓝: current_fill_color 为蓝",
      win.current_fill_color is not None
      and QColor(win.current_fill_color).name().lower() == "#007aff")
check("蓝色填充块高亮(蓝框)",
      "2px solid #007aff" in win.fill_buttons["#007aff"].styleSheet())
check("无填充块取消高亮",
      "2px solid #007aff" not in win.fill_buttons[None].styleSheet())
win._select_fill_color(None)
check("选无填充: current_fill_color 为 None", win.current_fill_color is None)
check("无填充块高亮(蓝框)",
      "2px solid #007aff" in win.fill_buttons[None].styleSheet())


def center_color(tool, fill):
    """fill: QColor=有填充; None=显式无填充(加键); 'absent'=老标注缺键。"""
    pm = QPixmap(100, 80)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    ann = {"tool": tool, "rect": QRect(10, 10, 80, 60),
           "color": QColor(255, 59, 48), "width": 4}
    if fill != "absent":
        ann["fill_color"] = None if fill is None else QColor(fill)
    win._draw_annotation(p, ann, 0, 0)
    p.end()
    return pm.toImage().pixelColor(50, 40)


# 无填充(显式 None 与老标注缺键)都应空心
c = center_color("rectangle", None)
check("显式无填充矩形: 内部透明", c.alpha() == 0, f"alpha={c.alpha()}")
c = center_color("rectangle", "absent")
check("向后兼容(缺 fill_color 键): 不崩且空心", c.alpha() == 0, f"alpha={c.alpha()}")
c = center_color("ellipse", "absent")
check("椭圆向后兼容(缺键): 不崩且空心", c.alpha() == 0, f"alpha={c.alpha()}")

# 有填充:内部半透明且偏该色
c = center_color("rectangle", "#007aff")
check("蓝色填充矩形: 内部半透明(alpha>100)", c.alpha() > 100, f"alpha={c.alpha()}")
check("蓝色填充矩形: 内部偏蓝(B>R)", c.blue() > c.red(),
      f"r{c.red()} g{c.green()} b{c.blue()}")
c = center_color("ellipse", "#34c759")
check("绿色填充椭圆: 内部半透明(alpha>100)", c.alpha() > 100, f"alpha={c.alpha()}")
check("绿色填充椭圆: 内部偏绿(G>R)", c.green() > c.red(),
      f"r{c.red()} g{c.green()} b{c.blue()}")

# 提交的标注数据带 fill_color 字段
win.current_fill_color = QColor("#ffcc00")
pm = QPixmap(100, 80)
pm.fill(Qt.GlobalColor.transparent)
p = QPainter(pm)
cur = {
    "tool": "rectangle",
    "rect": QRect(10, 10, 80, 60),
    "color": QColor(win.current_color),
    "fill_color": QColor(win.current_fill_color) if win.current_fill_color else None,
    "width": win.current_width,
}
check("提交数据 fill_color 为黄色", cur["fill_color"] is not None
      and cur["fill_color"].name().lower() == "#ffcc00")
p.end()

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
