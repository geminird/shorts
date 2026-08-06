"""无头验证：拖动(平移)工具。

触摸板无中键时，工具栏"拖动"模式用左键平移放大后的图像。本测试验证：
  1. 滚动结果视图下，pan 工具左键拖动 -> 平移(view_offset 改变)，不创建标注；
  2. pan 不写入 QSettings(临时模式，不覆盖上次绘图工具)；
  3. 非滚动结果视图下，pan 左键为 no-op(无缩放意义，不平移、不画标注)；
  4. 切换回绘图工具后，左键能正常进入绘制。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_pan_tool.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QRect, QPoint, QPointF, QEvent
from PyQt6.QtGui import QImage, QPixmap, QMouseEvent
from PyQt6.QtWidgets import QApplication

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


def _press(win, pt, btn=Qt.MouseButton.LeftButton):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pt), btn, btn,
                     Qt.KeyboardModifier.NoModifier)
    win.mousePressEvent(ev)


def _move(win, pt):
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(pt),
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier)
    win.mouseMoveEvent(ev)


def _release(win, pt, btn=Qt.MouseButton.LeftButton):
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(pt), btn, btn,
                     Qt.KeyboardModifier.NoModifier)
    win.mouseReleaseEvent(ev)


# ============ 1. 滚动结果视图：pan 左键拖动 =平移 ============
win = M.SelectionWindow()
win.show()
win._create_toolbar()  # _select_tool 依赖 tool_buttons
# 一张非空原图 -> _is_scroll_result() 为 True
win.scroll_original_pixmap = QPixmap.fromImage(
    QImage(400, 1200, QImage.Format.Format_RGBA8888))
win.scroll_original_pixmap.fill(Qt.GlobalColor.white)
win.selection_rect = QRect(0, 0, 400, 800)
win.selection_confirmed = True
win.view_zoom = 2.0
win.view_offset = QPointF(10.0, 10.0)
win.scroll_scale_factor = 1.0
win.annotations = []

# 先随便选个绘图工具写一下 QSettings，再选 pan，确认 pan 不覆盖它
win._select_tool("rectangle")
saved_before = M.QSettings("Shorts", "Shorts").value("tool", type=str)
win._select_tool("pan")
saved_after = M.QSettings("Shorts", "Shorts").value("tool", type=str)
check("pan 不写入 QSettings(保持上次绘图工具)", saved_after == saved_before and saved_after != "pan",
      f"before={saved_before} after={saved_after}")
check("current_tool 已切到 pan", win.current_tool == "pan")

offset0 = QPointF(win.view_offset)
_press(win, QPoint(100, 100))
check("pan 左键按下进入平移(panning=True)", win.panning is True)
check("pan 拖动中不创建标注", len(win.annotations) == 0)
_move(win, QPoint(140, 130))  # +40, +30
exp_x, exp_y = offset0.x() + 40, offset0.y() + 30
check("pan 平移改变 view_offset (+40,+30)",
      abs(win.view_offset.x() - exp_x) < 1 and abs(win.view_offset.y() - exp_y) < 1,
      f"got {win.view_offset.x()},{win.view_offset.y()} want {exp_x},{exp_y}")
_release(win, QPoint(140, 130))
check("pan 左键释放结束平移(panning=False)", win.panning is False)

# ============ 2. 非滚动结果视图：pan 左键为 no-op ============
win2 = M.SelectionWindow()
win2.show()
win2._create_toolbar()
win2.scroll_original_pixmap = None  # 非滚动结果
win2.selection_rect = QRect(0, 0, 2000, 2000)
win2.selection_confirmed = True
win2.current_tool = "pan"
win2.annotations = []
off0 = QPointF(win2.view_offset)
_press(win2, QPoint(100, 100))
_move(win2, QPoint(150, 150))
_release(win2, QPoint(150, 150))
check("非滚动视图: pan 不平移(view_offset 不变)", win2.view_offset == off0,
      f"{win2.view_offset.x()},{win2.view_offset.y()} vs {off0.x()},{off0.y()}")
check("非滚动视图: pan 不创建标注", len(win2.annotations) == 0)

# ============ 3. 切回绘图工具后能正常进入绘制 ============
win.current_tool = "arrow"
_press(win, QPoint(50, 50))
check("切回 arrow: 左键进入绘制(is_drawing=True)", getattr(win, "is_drawing", False) is True)
_release(win, QPoint(50, 50))

# ============ 4. 工具栏确实新增了 pan 按钮 ============
check("工具栏含 pan 按钮", "pan" in win.tool_buttons)
check("pan 按钮图标非空", not win.tool_buttons["pan"].icon().isNull())

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
