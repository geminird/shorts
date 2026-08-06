"""无头验证：标注命中(几何精确) + 重叠循环选中 + 新增可移动类型。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_hittest.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QRect, QPoint, QPointF, QEvent
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import QApplication

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


win = M.SelectionWindow()
win.selection_confirmed = True
win.selection_rect = QRect(0, 0, 2000, 2000)

# ---------- 1. 两个重叠矩形：应命中两个候选，最上层在前 ----------
r1 = {"tool": "rectangle", "rect": QRect(100, 100, 200, 100), "color": QColor(255, 0, 0), "width": 4}
r2 = {"tool": "rectangle", "rect": QRect(150, 120, 200, 100), "color": QColor(0, 0, 255), "width": 4}
win.annotations = [r1, r2]  # r2 后绘制 → 更靠上
center = QPoint(250, 160)  # 在两者之内
cands = win._hit_test_annotations(center)
check("重叠矩形命中两个候选", len(cands) == 2, f"got {len(cands)}")
check("最上层(r2)在候选首位", len(cands) == 2 and cands[0][0] is r2)
check("_hit_test_annotation 取最上层", win._hit_test_annotation(center)[0] is r2)

# ---------- 2. 同点循环：依次切到下层 ----------
win._cycle_anchor = None
win._cycle_idx = 0
check("第一次点 → r2", win._pick_candidate(center, cands)[0] is r2)
check("第二次点(同点) → r1", win._pick_candidate(center, cands)[0] is r1)
check("第三次点(同点) → 回到 r2", win._pick_candidate(center, cands)[0] is r2)
# 移到远处 → 重新从最上层开始(idx 归 0)
win._pick_candidate(QPoint(250, 600), [(r2, "rect")])
check("移到远处后 cycle_idx 归 0", win._cycle_idx == 0)

# ---------- 3. 箭头：沿线命中，而非整个包围盒 ----------
a1 = {"tool": "arrow", "start_pos": QPoint(100, 500), "end_pos": QPoint(500, 800),
      "color": QColor(0, 0, 0), "width": 4}
win.annotations = [a1]
on_line = QPoint(300, 650)          # 恰在对角线上(t=0.5)
off_in_bbox = QPoint(150, 780)      # 在旧包围盒内，但离线 ~194px
check("箭头线上命中", win._hit_test_annotation(on_line)[0] is a1)
check("箭头包围盒内但离线远 → 不命中(关键修复)", win._hit_test_annotation(off_in_bbox) is None)
ht_ep = win._hit_test_annotation(QPoint(103, 502))
check("箭头端点命中为 point", ht_ep is not None and ht_ep[1] == "point")

# ---------- 4. 画笔：折线命中 ----------
pen = {"tool": "pen", "rect": QRect(100, 700, 400, 20),
       "points": [QPoint(100, 710), QPoint(300, 710), QPoint(500, 710)],
       "color": QColor(0, 0, 0), "width": 4}
win.annotations = [pen]
check("画笔线上命中", win._hit_test_annotation(QPoint(200, 712))[0] is pen)
check("画笔远离线不命中", win._hit_test_annotation(QPoint(200, 750)) is None)

# ---------- 5. 高亮 / 马赛克 现在可命中(原先 _hit_test 根本没覆盖) ----------
hl = {"tool": "highlight", "rect": QRect(100, 800, 200, 80), "color": QColor(255, 255, 0), "width": 4}
win.annotations = [hl]
check("高亮可命中", win._hit_test_annotation(QPoint(150, 820))[0] is hl)
ms = {"tool": "mosaic", "rect": QRect(100, 900, 200, 80), "color": QColor(0, 0, 0), "width": 4}
win.annotations = [ms]
check("马赛克可命中", win._hit_test_annotation(QPoint(150, 930))[0] is ms)

# ---------- 6. _start_dragging 对 pen/highlight/mosaic 不报错且记录初始 ----------
for t, ann in [("pen", pen), ("highlight", hl), ("mosaic", ms)]:
    try:
        win._start_dragging(ann, t, QPoint(150, ann["rect"].y() + 10))
        ok, err = True, ""
    except Exception as e:  # noqa: BLE001
        ok, err = False, repr(e)
    check(f"_start_dragging[{t}] 无异常", ok, err)
    if t == "pen":
        check("_start_dragging[pen] 记录 drag_initial_points(3点)",
              hasattr(win, "drag_initial_points") and len(win.drag_initial_points) == 3)
    win.dragging_ann = None

# ---------- 7. 端到端拖动：pen 的 rect 与所有 points 整体平移 ----------
pen2 = {"tool": "pen", "rect": QRect(100, 700, 400, 20),
        "points": [QPoint(100, 710), QPoint(300, 710), QPoint(500, 710)],
        "color": QColor(0, 0, 0), "width": 4}
win.annotations = [pen2]
win._start_dragging(pen2, "pen", QPoint(200, 712))  # 起点在画笔上
# 鼠标右移 50、下移 30
ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(250, 742),
                 Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                 Qt.KeyboardModifier.NoModifier)
win.mouseMoveEvent(ev)
moved_rect = pen2["rect"]
check("拖动后 pen.rect 平移 (+50,+30)",
      moved_rect.x() == 150 and moved_rect.y() == 730 and moved_rect.width() == 400,
      f"rect={moved_rect.getRect()}")
exp_pts = [QPoint(150, 740), QPoint(350, 740), QPoint(550, 740)]
ok_pts = all(pen2["points"][k].x() == exp_pts[k].x() and pen2["points"][k].y() == exp_pts[k].y()
             for k in range(3))
check("拖动后 pen.points 全部整体平移", ok_pts, str([(p.x(), p.y()) for p in pen2["points"]]))
win.dragging_ann = None

# ---------- 8. 端到端拖动：highlight 仅 rect 平移 ----------
win.annotations = [hl]
win._start_dragging(hl, "highlight", QPoint(150, 820))
ev2 = QMouseEvent(QEvent.Type.MouseMove, QPointF(170, 820),
                  Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                  Qt.KeyboardModifier.NoModifier)
win.mouseMoveEvent(ev2)
check("拖动后 highlight.rect 平移 (+20,0)",
      hl["rect"].x() == 120 and hl["rect"].y() == 800 and hl["rect"].width() == 200,
      f"rect={hl['rect'].getRect()}")
win.dragging_ann = None

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
