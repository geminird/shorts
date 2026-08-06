"""无头验证：用 offscreen 平台验证本轮优化的各条新代码路径，
避免 windowed(console=False) 程序因事件处理器异常而静默崩溃。
运行： set QT_QPA_PLATFORM=offscreen  &&  py -3 _test_optimize.py
"""
import sys
from pathlib import Path

# 强制 offscreen（即便环境变量没设）
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, QSettings
from PyQt6.QtGui import QImage, QPixmap, QColor, QPainter
from PyQt6.QtWidgets import QApplication

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  -> " + extra) if extra and not cond else ""))


# ---------- 1. SelectionWindow 构造(QSettings + 多显示器几何 + 缓存状态) ----------
try:
    win = M.SelectionWindow()
    check("SelectionWindow() 构造无异常", True)
except Exception as e:
    win = None
    check("SelectionWindow() 构造无异常", False, repr(e))

if win is not None:
    check("__init__ 创建 QSettings", hasattr(win, "_settings"))
    check("__init__ 默认 current_tool 合法", win.current_tool in
          ("arrow", "rectangle", "ellipse", "text", "pen", "highlight", "mosaic", "sequence"),
          win.current_tool)
    check("__init__ 马赛克缓存初始化为空", win._mosaic_cache == {})
    check("__init__ 背景版本=0", win._bg_version == 0)
    geo = win.geometry()
    check("窗口覆盖虚拟桌面(宽高>0)", geo.width() > 0 and geo.height() > 0,
          f"{geo.width()}x{geo.height()}")


# ---------- 2. _virtual_desktop_geometry 单屏(offscreen)应等于唯一屏幕 ----------
vd = M._virtual_desktop_geometry()
check("_virtual_desktop_geometry 返回有效矩形", vd.width() > 0 and vd.height() > 0)
screens = app.screens()
if len(screens) == 1:
    check("单显示器下虚拟桌面 == 该屏 geometry", vd == screens[0].geometry(),
          f"{vd} vs {screens[0].geometry()}")


# ---------- 3. QSettings 持久化往返 ----------
if win is not None:
    try:
        if not hasattr(win, "tool_buttons"):
            win._create_toolbar()  # _select_tool 依赖 tool_buttons
        if not hasattr(win, "width_buttons"):
            win._create_style_panel()  # _select_width 依赖 width_buttons(已移入 style_panel)
        win._select_tool("pen")
        win._select_width(6)
        win._select_color(QColor("#007aff"), "#007aff")
        rs = QSettings("Shorts", "Shorts")
        check("QSettings 存 tool", rs.value("tool") == "pen", repr(rs.value("tool")))
        check("QSettings 存 width", int(rs.value("width")) == 6, repr(rs.value("width")))
        check("QSettings 存 color", rs.value("color") == "#007aff", repr(rs.value("color")))
    except Exception as e:
        check("QSettings 持久化往返", False, repr(e))


# ---------- 4. 马赛克缓存：构建 + 命中 ----------
if win is not None:
    bg = QPixmap(200, 200)
    bg.fill(QColor(120, 80, 200))
    p = QPainter(bg)
    for i in range(0, 200, 10):
        p.fillRect(QRect(i, 0, 5, 200), QColor(i, 255 - i, 100))
    p.end()
    win.set_background(bg)
    check("set_background 自增 bg_version", win._bg_version == 1, str(win._bg_version))
    check("set_background 清空马赛克缓存", win._mosaic_cache == {})

    block = win._build_mosaic_block(10, 10, 90, 90, 10)
    check("_build_mosaic_block 返回非空 pixmap", not block.isNull())

    ann = {"tool": "mosaic", "rect": QRect(10, 10, 80, 80)}
    out = QPixmap(200, 200)
    out.fill(Qt.GlobalColor.transparent)
    pp = QPainter(out)
    win._draw_annotation(pp, ann, 0, 0)
    pp.end()
    check("绘制马赛克后缓存命中(1条)", len(win._mosaic_cache) == 1,
          str(len(win._mosaic_cache)))
    cached = list(win._mosaic_cache.values())[0]
    check("缓存里的 pixmap 非空", not cached.isNull())

    # 再次绘制应命中缓存(不新增)
    before = len(win._mosaic_cache)
    out2 = QPixmap(200, 200)
    pp2 = QPainter(out2)
    win._draw_annotation(pp2, ann, 0, 0)
    pp2.end()
    check("重复绘制不新增缓存", len(win._mosaic_cache) == before, str(len(win._mosaic_cache)))


# ---------- 5. _stitch_images：合成两帧重叠 → 正确裁剪 ----------
def make_frame(W, H, seed):
    """非周期横向差异 + 纵向渐变，便于重叠检测。"""
    img = QImage(W, H, QImage.Format.Format_RGBA8888)
    for y in range(H):
        for x in range(0, W, 4):  # 每 4 像素同色，提速
            v = (x * 7 + y * 13 + seed * 31) & 0xFF
            c = QColor(v, (v * 3) & 0xFF, (255 - v) & 0xFF)
            for dx in range(4):
                if x + dx < W:
                    img.setPixelColor(x + dx, y, c)
    return img


W, H, OV = 120, 200, 60
f1 = make_frame(W, H, 1)
# 第二帧：内容向下平移 OV 行(模拟滚动)，顶部 OV 行 == f1 底部 OV 行
f2 = QImage(W, H, QImage.Format.Format_RGBA8888)
for y in range(H):
    src_y = y + OV  # f2 的第 y 行 == f1 的第 y+OV 行
    if src_y < H:
        for x in range(W):
            f2.setPixelColor(x, y, f1.pixelColor(x, src_y))
    else:
        # 新内容(滚进来的)
        for x in range(0, W, 4):
            v = (x * 5 + y * 11 + 99) & 0xFF
            c = QColor((v * 2) & 0xFF, v, 200)
            for dx in range(4):
                if x + dx < W:
                    f2.setPixelColor(x + dx, y, c)

stitched = M._stitch_images([f1, f2])
check("_stitch_images 返回非空", not stitched.isNull())
# f2 顶部 (H-OV) 行 == f1 底部 (H-OV) 行 → 检出重叠 ov=(H-OV)，结果高 = H + OV
check("_stitch_images 非生硬拼接(检测到重叠)", stitched.height() < 2 * H,
      f"got {stitched.height()} (生硬拼接应为 {2 * H})")
check("_stitch_images 裁剪正确(高==H+OV)", stitched.height() == H + OV,
      f"got {stitched.height()} want {H + OV}")
check("_stitch_images 宽度不变", stitched.width() == W)
# 单帧
one = M._stitch_images([f1])
check("_stitch_images 单帧高==H", one.height() == H, str(one.height()))


# ---------- 6. 滚动截图(_StitchWorker 已移除；抓帧用 _ScrollCaptureWorker+QThread) ----------
check("_StitchWorker 已移除", not hasattr(M, "_StitchWorker"), "仍存在 _StitchWorker")
# QThread 现用于子线程抓帧(_ScrollCaptureWorker)，保留导入是正确的
# 端到端：_finish_scroll_capture 同步走完(不再异步/线程)→ 结果图正确
if win is not None:
    win.scroll_frames = [QPixmap.fromImage(f1), QPixmap.fromImage(f2)]
    win.scroll_capture_rect = QRect(0, 0, W, H)
    win.is_scroll_capturing = True
    win.scroll_capture_bar = None
    win.scroll_capture_outline = None
    win._finish_scroll_capture()  # 同步：返回时拼接+切图已全部完成(不崩溃即过)
    stitched = getattr(win, "scroll_original_pixmap", None)
    check("_finish_scroll_capture 同步生成结果图",
          stitched is not None and not stitched.isNull(), repr(stitched))
    if stitched is not None and not stitched.isNull():
        check("_finish_scroll_capture 结果高==H+OV(去重叠)",
              stitched.height() == H + OV, f"{stitched.height()} want {H + OV}")


# ---------- 7. _on_stitch_finished：高图触发缩放 + 失效缓存 ----------
if win is not None:
    win._mosaic_cache["stale"] = QPixmap()  # 故意塞入旧缓存
    tall = QImage(W, 4000, QImage.Format.Format_RGBA8888)
    tall.fill(QColor(10, 20, 30))
    win.scroll_frames = []  # 避免干扰
    win._on_stitch_finished(tall)
    check("_on_stitch_finished 设置 scroll_original_pixmap",
          win.scroll_original_pixmap is not None and not win.scroll_original_pixmap.isNull())
    check("_on_stitch_finished 高图被缩放(scale_factor>1)", win.scroll_scale_factor > 1.0,
          str(win.scroll_scale_factor))
    check("_on_stitch_finished 自增 bg_version", win._bg_version >= 2, str(win._bg_version))
    check("_on_stitch_finished 清空马赛克缓存", win._mosaic_cache == {})


# ---------- 8. excepthook 写日志 ----------
try:
    M._install_excepthook()
    check("_install_excepthook 安装成功", sys.excepthook is not sys.__excepthook__)
    log_path = Path(M.__file__).parent / "shorts_error.log"
    if log_path.exists():
        log_path.unlink()
    sys.excepthook(ValueError, ValueError("boom-test"), None)
    check("excepthook 写入 shorts_error.log", log_path.exists() and "boom-test" in log_path.read_text(encoding="utf-8", errors="ignore"))
except Exception as e:
    check("_install_excepthook / 日志", False, repr(e))


# ---------- 9. 工具栏创建(撤销/重做按钮 + 图标 + 选中态同步) ----------
if win is not None:
    try:
        if not hasattr(win, "toolbar"):
            win._create_toolbar()
        ok_toolbar = True
        tb_err = ""
    except Exception as e:
        ok_toolbar = False
        tb_err = repr(e)
    check("_create_toolbar() 构造无异常", ok_toolbar, tb_err)
    if ok_toolbar:
        # 找到撤销/重做按钮(通过 tooltip)
        tips = []
        for child in win.toolbar.findChildren(type(win.toolbar.childAt(0, 0))) if False else []:
            pass
        # 主工具栏瘦身(task48):仅工具+操作按钮;样式组(色板/线宽/填充/字号/马赛克/序号)已移入 style_panel。
        # 故主工具栏 QPushButton = 9工具 + 撤销 + 重做 + 滚动 + 保存 + 关闭 + 完成 = 15
        from PyQt6.QtWidgets import QPushButton
        btns = win.toolbar.findChildren(QPushButton)
        n = len(btns)
        check("主工具栏按钮数(工具+操作, 瘦身后>=15)", n >= 15, f"n={n}")
        undo_icons = [b for b in btns if b.toolTip().startswith("撤销")]
        redo_icons = [b for b in btns if b.toolTip().startswith("重做")]
        check("存在撤销按钮", len(undo_icons) == 1, str(len(undo_icons)))
        check("存在重做按钮", len(redo_icons) == 1, str(len(redo_icons)))
        check("撤销图标非空", undo_icons and not undo_icons[0].icon().isNull())
        check("重做图标非空", redo_icons and not redo_icons[0].icon().isNull())


print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
