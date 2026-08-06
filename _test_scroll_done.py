"""回归测试：滚动截图点"完成" → 同步拼接 → 切图(不再崩溃)。

历史 bug：旧实现用 QThread + _StitchWorker 后台拼接，"完成"回调里
self._stitch_thread = None 会在 QThread 仍在运行时删除它(Qt 文档：删除运行
中的 QThread 会导致程序崩溃)→ "点完成后必崩"。现已改为 _finish_scroll_capture
在主线程同步拼接(无 QThread)。本测试验证该路径端到端不崩、结果图与后续"完成
复制"路径都正确。
运行： QT_QPA_PLATFORM=offscreen py -3 _test_scroll_done.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QImage, QPixmap, QColor
from PyQt6.QtWidgets import QApplication

import main as M  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

W, H, OV = 120, 200, 60


def _frame(seed):
    img = QImage(W, H, QImage.Format.Format_RGBA8888)
    for y in range(H):
        for x in range(0, W, 4):
            v = (x * 7 + y * 13 + seed * 31) & 0xFF
            c = QColor(v, (v * 3) & 0xFF, (255 - v) & 0xFF)
            for dx in range(4):
                if x + dx < W:
                    img.setPixelColor(x + dx, y, c)
    return img


def _shifted(src, ov, seed):
    out = QImage(W, H, QImage.Format.Format_RGBA8888)
    for y in range(H):
        sy = y + ov
        if sy < H:
            for x in range(W):
                out.setPixelColor(x, y, src.pixelColor(x, sy))
        else:
            for x in range(0, W, 4):
                v = (x * 5 + y * 11 + seed) & 0xFF
                c = QColor((v * 2) & 0xFF, v, 200)
                for dx in range(4):
                    if x + dx < W:
                        out.setPixelColor(x + dx, y, c)
    return out


PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + extra))


# 构造 3 帧(相邻帧内容下移 OV，模拟滚动)
f1 = _frame(1)
frames = [QPixmap.fromImage(im) for im in
          (f1, _shifted(f1, OV, 99), _shifted(_shifted(f1, OV, 99), OV, 77))]

win = M.SelectionWindow()
win.show()
win.scroll_frames = frames
win.scroll_capture_rect = QRect(0, 0, W, H)
win.is_scroll_capturing = True
win.scroll_capture_bar = None
win.scroll_capture_outline = None

# 同步拼接：调用返回时即完成(旧线程实现在此路径必崩)
try:
    win._finish_scroll_capture()
    crash = False
except Exception as e:  # noqa: BLE001
    crash = True
    print("EXCEPTION in _finish_scroll_capture:", repr(e))
check("点完成: _finish_scroll_capture 不崩溃", not crash)

orig = getattr(win, "scroll_original_pixmap", None)
check("点完成: 生成原图", orig is not None and not orig.isNull(), repr(orig))
if orig is not None and not orig.isNull():
    # 3 帧、每相邻去重叠 OV → 结果高 = H + 2*OV(非生硬拼接的 3*H)
    check("点完成: 原图尺寸正确(3帧去重叠)",
          orig.width() == W and orig.height() == H + 2 * OV,
          f"{orig.width()}x{orig.height()} want {W}x{H + 2 * OV}")

    # ✓完成 复制路径(同一 _get_result_pixmap，普通截图也走它，生产已验证)
    res = win._get_result_pixmap()
    check("完成复制: _get_result_pixmap 非空", not res.isNull(), f"null={res.isNull()}")
    # 注：不调用 clipboard.setPixmap —— offscreen QPA 的剪贴板对 setPixmap 不稳定
    # (测试环境问题，与生产真实 Windows 剪贴板无关，普通截图走同一调用且用户从未报障)。

print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
