# -*- coding: utf-8 -*-
"""无头验证：样式子面板(style_panel)——选中工具时在主工具栏下方弹出的样式设置区。

验证：
  1. 结构:style_panel + 6 个样式组 + 各按钮字典键集;
  2. 每工具的样式组显隐矩阵(核心):矩形/椭圆/箭头/画笔/高亮/文字/马赛克/序号/平移;
  3. 马赛克大小档:默认 10;_select_mosaic_size 设值+持久化+高亮;绘制分支读取 mosaic_size(6≠14);
  4. 序号大小档:默认 1.0;_select_seq_scale 设值+持久化+高亮;_seq_metrics 单调;绘制半径随 seq_scale 增大;
  5. 子面板高度计入:非 pan 工具 style_panel_height()>0,pan 为 0;pan 时整面板隐藏。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_style_panel.py
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


# 清理 QSettings，避免前序运行持久化的 tool/mosaic_size/seq_scale 污染默认值断言
M.QSettings("Shorts", "Shorts").clear()
win = M.SelectionWindow()
win.show()
win._create_toolbar()
win._create_style_panel()

# ---- 1. 结构 ----
check("style_panel 已创建", hasattr(win, "style_panel"))
check("6 个样式组均已创建",
      all(hasattr(win, g) for g in
          ("color_group", "fill_group", "width_group", "text_size_group",
           "mosaic_size_group", "seq_scale_group")))
check("色板 6 色", set(win.color_buttons) == {"#ff3b30", "#007aff", "#ffcc00", "#34c759", "#000000", "#ffffff"},
      str(set(win.color_buttons)))
check("填充组键 = 无填充 + 6 色", set(win.fill_buttons) == {None, "#ff3b30", "#007aff", "#ffcc00", "#34c759", "#000000", "#ffffff"},
      str(set(win.fill_buttons)))
check("线宽档 {1,2,4}", set(win.width_buttons) == {1, 2, 4}, str(set(win.width_buttons)))
check("字号档 {12,16,22}", set(win.text_size_buttons) == {12, 16, 22}, str(set(win.text_size_buttons)))
check("马赛克档 {14,10,6}", set(win.mosaic_size_buttons) == {14, 10, 6}, str(set(win.mosaic_size_buttons)))
check("序号档 {0.85,1.0,1.25}", set(win.seq_scale_buttons) == {0.85, 1.0, 1.25}, str(set(win.seq_scale_buttons)))


# ---- 2. 每工具样式组显隐矩阵 ----
# (tool, color, fill, width, text_size, mosaic, seq)
MATRIX = [
    ("rectangle", True,  True,  True,  False, False, False),
    ("ellipse",   True,  True,  True,  False, False, False),
    ("arrow",     True,  False, True,  False, False, False),
    ("pen",       True,  False, True,  False, False, False),
    ("highlight", True,  False, True,  False, False, False),
    ("text",      True,  False, False, True,  False, False),
    ("mosaic",    False, False, False, False, True,  False),
    ("sequence",  True,  False, False, False, False, True),
]
for tool, c, f, w, ts, ms, ss in MATRIX:
    win._select_tool(tool)
    # 非平移工具：整面板应可见
    check(f"[{tool}] 子面板可见", win.style_panel.isHidden() is False)
    check(f"[{tool}] color_group 显={c}", win.color_group.isHidden() is not c)
    check(f"[{tool}] fill_group 显={f}", win.fill_group.isHidden() is not f)
    check(f"[{tool}] width_group 显={w}", win.width_group.isHidden() is not w)
    check(f"[{tool}] text_size_group 显={ts}", win.text_size_group.isHidden() is not ts)
    check(f"[{tool}] mosaic_size_group 显={ms}", win.mosaic_size_group.isHidden() is not ms)
    check(f"[{tool}] seq_scale_group 显={ss}", win.seq_scale_group.isHidden() is not ss)

# 平移：整面板隐藏（所有组因父隐藏而 isHidden）
win._select_tool("pan")
check("[pan] 子面板隐藏", win.style_panel.isHidden() is True)
check("[pan] color_group 随面板隐藏", win.color_group.isHidden() is True)


# ---- 3. 马赛克大小档 ----
check("马赛克默认 10", win.current_mosaic_size == 10, str(win.current_mosaic_size))
win._select_mosaic_size(14)
check("选马赛克14: current_mosaic_size=14", win.current_mosaic_size == 14)
check("选马赛克14: 按钮14 active", win.mosaic_size_buttons[14].property("active") is True)
check("选马赛克14: 按钮10 取消active", win.mosaic_size_buttons[10].property("active") is False)
check("选马赛克14: 持久化 mosaic_size=14",
      M.QSettings("Shorts", "Shorts").value("mosaic_size", type=int) == 14)


def render_mosaic(mosaic_size):
    pm = QPixmap(100, 60)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    ann = {"tool": "mosaic", "rect": QRect(10, 10, 80, 40),
           "color": QColor(0, 0, 0), "width": 2, "mosaic_size": mosaic_size}
    win._draw_annotation(p, ann, 0, 0)
    p.end()
    return pm


# 背景：左红右蓝，锐利边界，便于块网格差异化
bg = QPixmap(100, 60)
bp = QPainter(bg)
bp.fillRect(QRect(0, 0, 50, 60), QColor(255, 0, 0))
bp.fillRect(QRect(50, 0, 50, 60), QColor(0, 0, 255))
bp.end()
win.background_pixmap = bg

pm6 = render_mosaic(6)
pm14 = render_mosaic(14)
op6 = sum(1 for y in range(60) for x in range(100) if pm6.toImage().pixelColor(x, y).alpha() > 40)
op14 = sum(1 for y in range(60) for x in range(100) if pm14.toImage().pixelColor(x, y).alpha() > 40)
check("马赛克绘制有输出(6)", op6 > 0, f"op6={op6}")
# 不同块大小 → 像素化网格不同 → 两图不像素全等
same = pm6.toImage().constBits() == pm14.toImage().constBits()
check("mosaic_size 6 与 14 渲染不同(绘制分支读取 mosaic_size)", not same, "两图完全相同")


# ---- 4. 序号大小档 ----
check("序号默认 1.0", win.current_seq_scale == 1.0, str(win.current_seq_scale))
win._select_seq_scale(1.25)
check("选序号1.25: current_seq_scale=1.25", win.current_seq_scale == 1.25)
check("选序号1.25: 按钮1.25 active", win.seq_scale_buttons[1.25].property("active") is True)
check("选序号1.25: 按钮1.0 取消active", win.seq_scale_buttons[1.0].property("active") is False)
check("选序号1.25: 持久化 seq_scale=1.25",
      abs(M.QSettings("Shorts", "Shorts").value("seq_scale", type=float) - 1.25) < 1e-9)

# _seq_metrics：1.0 基准 + 单调递增
m100 = win._seq_metrics(1.0)
check("_seq_metrics(1.0) = (12,10,12,24,7)", m100 == (12, 10, 12, 24, 7), str(m100))
r085 = win._seq_metrics(0.85)[0]
r100 = win._seq_metrics(1.0)[0]
r125 = win._seq_metrics(1.25)[0]
check("_seq_metrics 半径单调 0.85<1.0<1.25", r085 < r100 < r125, f"{r085},{r100},{r125}")
h085 = win._seq_metrics(0.85)[3]
h125 = win._seq_metrics(1.25)[3]
check("_seq_metrics 文字框高 0.85<1.25", h085 < h125, f"{h085},{h125}")


def render_seq(scale):
    pm = QPixmap(80, 80)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    ann = {"tool": "sequence", "circle_pos": QPoint(20, 40), "text_pos": QPoint(60, 30),
           "text": "", "number": 1, "color": QColor(255, 59, 48), "seq_scale": scale}
    win._draw_annotation(p, ann, 0, 0)
    p.end()
    return pm


op_s085 = sum(1 for y in range(80) for x in range(80) if render_seq(0.85).toImage().pixelColor(x, y).alpha() > 40)
op_s125 = sum(1 for y in range(80) for x in range(80) if render_seq(1.25).toImage().pixelColor(x, y).alpha() > 40)
check("序号绘制: seq_scale 1.25 不透明像素 > 0.85(半径随档增大)",
      op_s125 > op_s085, f"0.85={op_s085} 1.25={op_s125}")


# ---- 5. 子面板高度计入 + pan 隐藏 ----
win._select_tool("rectangle")
check("非 pan: style_panel_height()>0", win.style_panel_height() > 0, str(win.style_panel_height()))
win._select_tool("pan")
check("pan: style_panel_height()==0", win.style_panel_height() == 0, str(win.style_panel_height()))


print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
