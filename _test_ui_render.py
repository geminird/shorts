"""无头验证：UI 精细打磨后的图标系统与工具栏。

验证要点：
  1. _create_toolbar() 不抛异常；
  2. 15 个工具图标 + 线宽图标均返回非空 QIcon；
  3. 调色板生效：中性图标(arrow/rect/.../save/pan 等)像素呈灰色(低色差)，
     close 呈红、check 呈绿(高色差)——证明旧的"红箭头/蓝形状/绿保存"已统一为中性；
  4. 线宽图标由细到粗：w=2/4/6 的不透明像素数严格递增；
  5. 线宽按钮改为图标(无文字)、9 个工具齐全、pan 可选。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_ui_render.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


def opaque_colors(icon):
    """返回图标(24x24)所有不透明像素的 (r,g,b) 列表。"""
    if icon.isNull():
        return []
    img = icon.pixmap(24, 24).toImage()
    cols = []
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 40:
                cols.append((c.red(), c.green(), c.blue()))
    return cols


def max_spread(cols):
    """像素集中最大 R-G-B 色差(越大越"鲜艳/带色")。"""
    if not cols:
        return -1
    return max(max(r, g, b) - min(r, g, b) for r, g, b in cols)


win = M.SelectionWindow()
win._create_toolbar()  # 不抛异常即说明图标/线宽/样式接线正确
win._create_style_panel()  # width_buttons 等样式组已移入 style_panel
check("工具栏构建不抛异常", True)

# ---- 1+2. 所有图标非空 ----
NEUTRAL = ["arrow", "rect", "ellipse", "text", "pen", "highlight",
           "mosaic", "sequence", "pan", "undo", "redo", "scroll", "save"]
icon_objs = {}
for n in NEUTRAL + ["close", "check"]:
    fn = getattr(win, f"_create_{n}_icon", None)
    ic = fn() if fn else None
    icon_objs[n] = ic
    check(f"图标非空: {n}", ic is not None and not ic.isNull())
for w in (2, 4, 6):
    ic = win._create_width_icon(w)
    icon_objs[f"width{w}"] = ic
    check(f"线宽图标非空: w={w}", not ic.isNull())

# ---- 3. 调色板：中性=低色差，close=红、check=绿=高色差 ----
for n in NEUTRAL:
    sp = max_spread(opaque_colors(icon_objs[n]))
    check(f"中性低色差: {n} (spread={sp})", 0 <= sp < 45, f"spread={sp}")
sp_close = max_spread(opaque_colors(icon_objs["close"]))
sp_check = max_spread(opaque_colors(icon_objs["check"]))
check("close 呈红色(高色差)", sp_close > 90, f"spread={sp_close}")
check("check 呈绿色(高色差)", sp_check > 90, f"spread={sp_check}")
# close 偏红(R 最大)、check 偏绿(G 最大)
cc_close = opaque_colors(icon_objs["close"])
cc_check = opaque_colors(icon_objs["check"])
if cc_close:
    r, g, b = max(cc_close, key=lambda c: c[0] - c[1])
    check("close 最红像素 R>G,B", r > g + 40 and r > b + 40, f"{r},{g},{b}")
if cc_check:
    r, g, b = max(cc_check, key=lambda c: c[1] - c[0])
    check("check 最绿像素 G>R,B", g > r + 40 and g > b + 20, f"{r},{g},{b}")

# ---- 4. 线宽图标由细到粗：不透明像素数递增 ----
counts = {w: len(opaque_colors(icon_objs[f"width{w}"])) for w in (2, 4, 6)}
check("线宽由细到粗(像素数 2<4<6)",
      counts[2] < counts[4] < counts[6], f"{counts}")

# ---- 5. 线宽按钮改为图标(无文字)；9 个工具齐全；pan 可选 ----
for w, btn in win.width_buttons.items():
    check(f"线宽按钮无文字(图标化) w={w}", btn.text() == "", repr(btn.text()))
expected_tools = {"arrow", "rectangle", "ellipse", "text", "pen", "highlight",
                  "mosaic", "sequence", "pan"}
check("9 个工具按钮齐全", expected_tools <= set(win.tool_buttons),
      str(set(win.tool_buttons)))
win._select_tool("pan")
check("pan 工具可选且高亮(active)", win.current_tool == "pan"
      and win.tool_buttons["pan"].property("active") is True)
win._select_tool("arrow")
check("切回 arrow 生效", win.current_tool == "arrow"
      and win.tool_buttons["arrow"].property("active") is True)

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
