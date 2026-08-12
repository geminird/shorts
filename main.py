"""Shorts - 截图标注工具 - 沉浸式体验类似iShot"""
import sys
import math
import copy
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QFileDialog, QSizePolicy, QLineEdit, QSystemTrayIcon, QMenu, QCheckBox
)
from PyQt6.QtCore import (
    Qt, QPoint, QRect, QRectF, QPointF, pyqtSignal, QTimer, QSize,
    QObject, QEvent, QSettings, QThread,
)
from PyQt6.QtGui import (
    QPixmap, QPainter, QColor, QPen, QBrush, QCursor, QImage,
    QFont, QFontMetrics, QIcon, QWindow, QPainterPath, QAction, QGuiApplication,
)

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from core.screenshot import Screenshot
from utils.platform import is_macos, overlay_window_flags, raise_overlay, activate_foreground_app, make_mouse_passthrough, make_floating_panel, device_pixel_ratio
from PIL import Image


def _install_excepthook():
    """安装全局异常钩子。

    本程序以 console=False(windowed) 打包，没有 stderr。事件处理器
    (paintEvent/槽函数等)里未捕获的 Python 异常会让进程"直接崩溃"且无堆栈
    (历史上一次漏 import 的 NameError 就是这么表现为"点完成后闪退")。
    这里把 traceback 写入 shorts_error.log，便于事后定位根因。
    """
    import traceback as _tb
    from datetime import datetime

    def _log_path():
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent
        return base / "shorts_error.log"

    def _hook(exc_type, exc_value, exc_tb):
        try:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n")
                _tb.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        # 仍交给默认钩子(开发态可打印到控制台)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def _virtual_desktop_geometry():
    """所有显示器的并集矩形(虚拟桌面)，用于跨多显示器截图。

    单显示器时即等于主屏 geometry，保证零回归。坐标系与 mss 的
    monitors[0](整块虚拟屏)一致：窗口局部坐标(0,0) == 虚拟桌面原点。
    """
    screens = QGuiApplication.screens()
    if not screens:
        primary = QGuiApplication.primaryScreen()
        return primary.geometry() if primary else QRect(0, 0, 1, 1)
    geo = QRect(screens[0].geometry())
    for s in screens[1:]:
        geo = geo.united(s.geometry())
    return geo


# ---- 滚动帧缝合：纯函数，不访问 self、不碰 QPixmap，可在工作线程安全调用 ----
def _row_color_sums(data, W, H, row_size, col_stride):
    """每行采样列的 (R+G+B) 之和作为该行签名，返回 list[int]，长度 H。
    采样列用于大幅降低计算量，同时保留足够的横向区分度。"""
    sigs = []
    for y in range(H):
        base = y * row_size
        s = 0
        for x in range(0, W, col_stride):
            o = base + x * 4
            s += data[o] + data[o + 1] + data[o + 2]  # R+G+B(忽略 A)
        sigs.append(s)
    return sigs


def _bytes_diff_ratio(a, b, threshold=24):
    """两帧 RGBA 字节的内容差异占比（采样）：差异像素数 / 采样像素数。

    用于滚动截图去重：macOS 平滑滚动产生亚像素动画中间帧，逐字节比较永远
    不等；这里按通道差之和 > threshold 判为"该像素变化"，返回变化像素占比。
    采样步长控制开销（每 4 像素取 1）。两帧长度不等返回 1.0（视为完全不同）。
    """
    n = len(a)
    if n != len(b) or n == 0:
        return 1.0
    step = 16  # 字节步长 ≈ 每 4 像素采样一次
    changed = 0
    total = 0
    for o in range(0, n - 3, step):
        d = abs(a[o] - b[o]) + abs(a[o + 1] - b[o + 1]) + abs(a[o + 2] - b[o + 2])
        if d > threshold:
            changed += 1
        total += 1
    if total == 0:
        return 1.0
    return changed / total



def _rgba_bytes_to_gray_np(data, W, H, ds=4):
    """把 RGBA 字节降采样为灰度 numpy 数组(用于快速 NCC 匹配)。

    ds=4：宽高各除以 4(LANCZOS 降采样的近似，但这里用近邻步长足够——拼接只
    需要找到对齐行，亚像素精度无关)。返回 (H//ds, W//ds) 的 float64 数组。
    """
    import numpy as np
    row_size = W * 4
    H2 = H // ds
    W2 = W // ds
    out = np.empty((H2, W2), dtype=np.float64)
    for y in range(H2):
        base = (y * ds) * row_size
        for x in range(W2):
            o = base + (x * ds) * 4
            # 亮度近似 0.299R + 0.587G + 0.114B
            out[y, x] = 0.299 * data[o] + 0.587 * data[o + 1] + 0.114 * data[o + 2]
    return out


def _find_scroll_overlap_np(prev_bytes, new_bytes, W, Hp, Hn):
    """用 cv2.matchTemplate (TM_CCOEFF_NORMED) + Sobel 边缘 + Lowe ratio test 检测重叠。

    参考成熟工具（mate-matt/screenshot-stitcher、xutianyi1999/scrollshot）的做法：
    - Sobel 边缘特征图代替原始像素（文字页面上更精确，一行错位也能检测到）
    - NCC 归一化互相关（亮度/对比度不变，抗亚像素渲染差异）
    - Lowe ratio test（最佳峰值必须明显超过次佳，否则拒绝——抗重复模式）
    """
    import numpy as np
    import cv2

    pa = np.frombuffer(prev_bytes, dtype=np.uint8).reshape(Hp, W, 4)
    na = np.frombuffer(new_bytes, dtype=np.uint8).reshape(Hn, W, 4)

    # 灰度
    prev_gray = cv2.cvtColor(pa, cv2.COLOR_RGBA2GRAY)
    new_gray = cv2.cvtColor(na, cv2.COLOR_RGBA2GRAY)

    # Sobel 边缘特征（文字行检测更精确）
    prev_edge = cv2.Sobel(prev_gray, cv2.CV_32F, 0, 1, ksize=3)
    new_edge = cv2.Sobel(new_gray, cv2.CV_32F, 0, 1, ksize=3)
    # 归一化到 0-255
    prev_edge = np.clip(prev_edge, 0, 255).astype(np.uint8)
    new_edge = np.clip(new_edge, 0, 255).astype(np.uint8)

    # 模板：prev 底部自适应高度
    win = max(20, min(Hp // 4, 80))
    tmpl = prev_edge[Hp - win:]  # (win, W)

    # matchTemplate 在 new_edge 中搜索模板
    res = cv2.matchTemplate(new_edge, tmpl, cv2.TM_CCOEFF_NORMED)
    # res shape: (Hn - win + 1, W - W + 1) = (Hn - win + 1, 1) → 只关心 y 方向
    res_flat = res.flatten()  # 每个位置 y 的 NCC 分数

    if len(res_flat) == 0:
        return 0

    # 找最佳和次佳
    sorted_idx = np.argsort(res_flat)[::-1]  # 降序
    best_y = sorted_idx[0]
    best_val = res_flat[best_y]
    second_val = res_flat[sorted_idx[1]] if len(sorted_idx) > 1 else 0

    # Lowe ratio test：最佳必须明显超过次佳（差值 > 0.05）
    if best_val - second_val < 0.05:
        return 0  # 歧义匹配，拒绝

    # NCC 分数太低
    if best_val < 0.5:
        return 0

    # best_y = 模板在 new 中的起始行
    # → 滚动量 = (Hp - win) - best_y
    scroll_px = (Hp - win) - best_y
    if scroll_px <= 0:
        return max(0, Hp - 1)

    # 像素精确微调：在 NCC 最佳位置 ±2 行用全像素 SAD 找最精确值
    pa_rgb = pa[:, :, :3].astype(np.int32)
    na_rgb = na[:, :, :3].astype(np.int32)
    tmpl_rgb = pa_rgb[Hp - win:]
    best_diff = float('inf')
    best_y_fine = best_y
    for delta in range(-2, 3):
        pos = best_y + delta
        if pos < 0 or pos + win > Hn:
            continue
        d = np.abs(na_rgb[pos:pos + win] - tmpl_rgb).mean()
        if d < best_diff:
            best_diff = d
            best_y_fine = pos

    scroll_px = (Hp - win) - best_y_fine
    if scroll_px <= 0:
        return max(0, Hp - 1)
    overlap = Hp - scroll_px
    overlap = max(0, min(overlap, Hp))
    # [诊断]
    try:
        import pathlib
        p = pathlib.Path("/tmp/stitch_ov.log")
        prev_t = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(prev_t + f"ov={overlap} keep={Hp-overlap} ncc={best_val:.3f} ratio_gap={best_val-second_val:.3f}\n", encoding="utf-8")
    except Exception:
        pass
    return overlap





def _find_scroll_overlap(psig, nsig, Hp, Hn, prev_bytes, new_bytes, W, col_stride):
    """求相邻两帧重叠行数(对外保留旧签名，内部走 numpy NCC)。

    若 numpy 不可用或 NCC 失败，返回 0(调用方整帧追加，安全降级)。
    """
    try:
        ov = _find_scroll_overlap_np(prev_bytes, new_bytes, W, Hp, Hn)
    except Exception:
        ov = 0
    return ov




def _stitch_images(images):
    """拼接 QImage 列表为长图(基于内容重叠检测，自动去除相邻帧重复部分)。

    纯函数：不访问 self、不触碰 QPixmap，仅对 QImage/字节做计算，返回
    已 detach 的 QImage，因此可在工作线程里安全调用。
    """
    if not images:
        return QImage()

    items = []
    for img in images:
        img = img.convertToFormat(QImage.Format.Format_RGBA8888).copy()
        bits = img.bits()
        bits.setsize(img.sizeInBytes())
        items.append((bytes(bits), img.width(), img.height()))

    W = items[0][1]
    row_size = W * 4

    if len(items) == 1:
        h = items[0][2]
        return QImage(items[0][0], W, h, row_size, QImage.Format.Format_RGBA8888).copy()

    col_stride = max(1, W // 32)
    # 预计算每帧行签名(宽度一致才计算)
    sigs = [_row_color_sums(data, W, _h, row_size, col_stride) if w == W else None
            for (data, w, _h) in items]

    result_bytes = bytearray(items[0][0])
    result_h = items[0][2]

    for i in range(1, len(items)):
        prev_bytes, _pw, prev_h = items[i - 1]
        new_bytes, new_w, new_h = items[i]
        # 宽度不一致或缺签名 → 容错直接拼接
        if new_w != W or sigs[i] is None or sigs[i - 1] is None:
            result_bytes.extend(new_bytes)
            result_h += new_h
            continue

        ov = _find_scroll_overlap(
            sigs[i - 1], sigs[i], prev_h, new_h,
            prev_bytes, new_bytes, W, col_stride
        )
        keep = new_h - ov
        if ov == 0 or keep < 10:
            continue
        # 拼接处渐变混合（消除亚像素偏移导致的黑线/错位）：
        # 在 ov 行前 4 行做线性过渡，从 prev 内容渐变到 new 内容
        blend_rows = min(4, ov // 2)
        if blend_rows > 0 and ov > blend_rows:
            import numpy as np
            blend_start = ov - blend_rows
            # prev 底部 blend_rows 行（已在 result 中）
            prev_tail_start = len(result_bytes) - blend_rows * row_size
            prev_tail = np.frombuffer(
                bytes(result_bytes[prev_tail_start:]), dtype=np.uint8).astype(np.int32)
            # new 对应的行
            new_blend = np.frombuffer(
                new_bytes[blend_start * row_size : ov * row_size],
                dtype=np.uint8).astype(np.int32)
            if len(prev_tail) == len(new_blend):
                # 线性混合：alpha 从 1.0(prev) 渐变到 0.0(new)
                for br in range(blend_rows):
                    a = 1.0 - br / blend_rows  # 1.0 → 0.0
                    s = br * row_size
                    e = (br + 1) * row_size
                    mixed = (prev_tail[s:e] * a + new_blend[s:e] * (1 - a)).astype(np.uint8)
                    result_bytes[prev_tail_start + s : prev_tail_start + e] = mixed.tobytes()
        # 追加 new 中重叠行之后的新内容（跳过第 0 行——可能是截图边缘伪影）
        result_bytes.extend(new_bytes[ov * row_size:])
        result_h += (new_h - ov)

    return QImage(
        bytes(result_bytes), W, result_h, row_size, QImage.Format.Format_RGBA8888
    ).copy()


from core.hotkey import HotkeyManager


class ShortsApp:
    """Shorts 应用主控类 - 管理系统托盘和全局快捷键"""

    def __init__(self):
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication(sys.argv)
            self.app.setStyle("Fusion")

        # 关键：禁止关闭窗口时退出应用
        self.app.setQuitOnLastWindowClosed(False)

        self.selection_window = None
        self.tray_icon = None
        self.hotkey_manager = None
        self._screenshot_cancelled = False  # 标记截图是否被取消

        self._setup_tray()
        # 窗口/任务栏图标与托盘一致(否则 exe 用默认图标)
        if self.tray_icon is not None and not self.tray_icon.icon().isNull():
            self.app.setWindowIcon(self.tray_icon.icon())
        self._register_hotkey()

    def _setup_tray(self):
        """设置系统托盘"""
        # 创建托盘图标
        self.tray_icon = QSystemTrayIcon(self.app)

        # 尝试加载图标
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # 打包环境：从 _MEIPASS 获取资源
            base_path = Path(sys._MEIPASS)
        else:
            # 开发环境
            base_path = Path(__file__).parent
        icon_paths = [
            base_path / "resources" / "icons" / "app.png",
            base_path / "resources" / "icons" / "app.ico",
        ]
        icon_loaded = False
        for icon_path in icon_paths:
            if icon_path.exists():
                self.tray_icon.setIcon(QIcon(str(icon_path)))
                icon_loaded = True
                break

        # 如果没有找到图标，使用系统托盘图标
        if not icon_loaded:
            # 创建一个简单的默认图标
            from PyQt6.QtGui import QPixmap, QPainter, QColor
            pixmap = QPixmap(32, 32)
            pixmap.fill(QColor(100, 150, 255))
            self.tray_icon.setIcon(QIcon(pixmap))

        # 创建托盘菜单
        menu = QMenu()

        # 开机自启
        self.autostart_action = QAction("开机自启", menu, checkable=True)
        self.autostart_action.setChecked(self._is_autostart_enabled())
        self.autostart_action.triggered.connect(self._toggle_autostart)
        menu.addAction(self.autostart_action)

        menu.addSeparator()

        _hk_label = " (双击 Cmd 键)" if is_macos() else " (Ctrl+Alt+A)"
        screenshot_action = QAction(f"截图{_hk_label}", menu)
        screenshot_action.triggered.connect(self.start_screenshot)
        menu.addAction(screenshot_action)

        menu.addSeparator()

        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.setToolTip("Shorts - 截图工具")
        self.tray_icon.show()

    def _is_autostart_enabled(self):
        """检查是否开启了开机自启"""
        from core import autostart
        return autostart.is_enabled()

    def _toggle_autostart(self, checked):
        """切换开机自启状态"""
        from core import autostart
        if checked:
            autostart.enable()
        else:
            autostart.disable()

    def _on_tray_activated(self, reason):
        # 不在单击托盘图标时直接触发截图——避免点图标想开菜单却误触截图。
        # 截图只通过菜单项或全局快捷键激活。
        pass

    def _register_hotkey(self):
        """注册全局快捷键"""
        # 使用热键管理器（平台实现见 core/hotkey.py）
        self.hotkey_manager = HotkeyManager(self.start_screenshot)
        self.hotkey_manager.start()

    def start_screenshot(self):
        """开始截图"""
        if self.selection_window is not None:
            try:
                self.selection_window.close()
            except:
                pass

        self.selection_window = SelectionWindow()
        self.selection_window.finished.connect(self._on_screenshot_finished)
        self.selection_window.show()
        self.selection_window.start_capture()

    def _on_screenshot_finished(self):
        # 检查是否被取消
        if self.selection_window is not None:
            if hasattr(self.selection_window, 'cancelled') and self.selection_window.cancelled:
                self._screenshot_cancelled = True
        self.selection_window = None

    def run(self):
        if QApplication.instance() is None:
            self.app = QApplication(sys.argv)
        return self.app.exec()


class _ScrollDimOverlay(QWidget):
    """滚动截图时的高亮遮罩。

    在捕获区域之外绘制半透明黑色遮罩 + 蓝色边框，复刻正常选区的"高亮模式"。
    关键点：
    - WA_TransparentForMouseEvents：不抢焦点、不阻挡滚轮，用户滚轮直达目标程序；
    - 捕获区域内部不绘制任何像素（遮罩只画在选区外、边框画在选区外沿），
      因此 mss 截到的每一帧都是干净画面，不会被遮罩/边框污染。
    """
    def __init__(self, capture_rect):
        super().__init__()
        self._rect = QRect(capture_rect)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # 多显示器：遮罩覆盖整个虚拟桌面
        self.setGeometry(_virtual_desktop_geometry())

    def paintEvent(self, event):
        if not self._rect.isValid():
            return
        painter = QPainter(self)
        r = self._rect
        w = self.width()
        h = self.height()
        # 选区外四块半透明遮罩
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 220))
        if r.top() > 0:
            painter.drawRect(0, 0, w, r.top())                                   # 上
        if r.bottom() < h - 1:
            painter.drawRect(0, r.bottom() + 1, w, h - r.bottom() - 1)           # 下
        if r.left() > 0:
            painter.drawRect(0, r.top(), r.left(), r.height())                   # 左
        if r.right() < w - 1:
            painter.drawRect(r.right() + 1, r.top(), w - r.right() - 1, r.height())  # 右
        # 蓝色边框：整体外移到选区外沿，确保不会出现在被捕获的帧里
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#0a84ff"), 2))
        painter.drawRect(r.adjusted(-2, -2, 2, 2))


class _ScrollCaptureWorker(QObject):
    """滚轮事件驱动的抓帧（macshot 方案）。

    用 NSEvent global monitor 监听滚轮事件：
    - 滚轮事件触发 → 150ms 节流抓帧（避免一次滚动抓太多帧）
    - 滚动停止 300ms 后 → 抓最后一帧（确保画面稳定）
    - 不滚动时不抓帧（天然内容驱动，帧间重叠由用户滚动节奏决定）
    需要「输入监控」权限。
    """
    frame_captured = pyqtSignal(bytes, int, int, int, int)  # data, x, y, w, h

    def __init__(self, rect):
        super().__init__()
        self._rect = QRect(rect)
        self._running = True
        self._scroll_monitor = None
        self._scroll_active = False  # 当前是否在滚动
        self._scroll_start_time = 0  # 滚动开始时间（第一次事件）
        self._last_capture_time = 0  # 最后一次抓帧的时间戳

    def run(self):
        from core.screenshot import Screenshot
        shot = Screenshot()
        while self._running:
            try:
                if not self._rect.isValid():
                    break
                frame = shot.capture_region(
                    self._rect.x(), self._rect.y(),
                    self._rect.width(), self._rect.height(),
                    use_2x=True
                )
                img = frame.toImage().convertToFormat(QImage.Format.Format_RGBA8888).copy()
                bits = img.bits()
                bits.setsize(img.sizeInBytes())
                data = bytes(bits)
                self.frame_captured.emit(
                    data, self._rect.x(), self._rect.y(),
                    img.width(), img.height()
                )
            except Exception:
                pass
            _thread_sleep(0.03)  # 30ms 固定间隔（去重逻辑过滤静止帧）

    def _install_scroll_monitor(self):
        """装 NSEvent global scrollWheel monitor。"""
        import sys as _sys
        if _sys.platform != "darwin":
            return
        try:
            from AppKit import NSEvent, NSEventMaskScrollWheel
        except Exception:
            return
        import time as _time
        try:
            def on_scroll(event):
                now = _time.monotonic()
                if not self._scroll_active:
                    self._scroll_active = True
                    self._scroll_start_time = now
                self._scroll_last_event = now
                # [诊断] 确认滚轮事件被接收
                try:
                    import pathlib
                    p = pathlib.Path("/tmp/scroll_events.log")
                    prev = p.read_text(encoding="utf-8") if p.exists() else ""
                    p.write_text(prev + f"scroll @ {now:.2f}\n", encoding="utf-8")
                except Exception:
                    pass

            self._scroll_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskScrollWheel, on_scroll
            )
            print(f"滚轮监听器已安装: {self._scroll_monitor is not None}")
        except Exception as e:
            print(f"滚轮监听器安装失败: {e}")

    def stop(self):
        self._running = False
        # 卸载滚轮监听器
        if self._scroll_monitor is not None:
            try:
                from AppKit import NSEvent
                NSEvent.removeMonitor_(self._scroll_monitor)
            except Exception:
                pass
            self._scroll_monitor = None


def _thread_sleep(seconds):
    import time
    time.sleep(seconds)


class _GifAnnotationOverlay(QWidget):
    """GIF 录制中的透明标注覆盖层。

    覆盖录制选区，接收鼠标事件让用户画矩形/箭头标注。
    默认鼠标穿透（不影响用户操作目标应用），选了标注工具后变为可交互。
    画完的标注通过 GifRecorder.add_annotation 存入列表，叠加到每一帧。
    """

    def __init__(self, rect, recorder):
        super().__init__()
        self._rect = QRect(rect)
        self._recorder = recorder
        self._tool = None  # "rect" / "arrow" / None
        self._color = QColor(255, 59, 48)
        self._start = QPoint()
        self._current = QPoint()
        self._drawing = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # 默认穿透
        self.setGeometry(rect)

    def showEvent(self, event):
        """show 后设 native 层鼠标穿透（仅首次，避免 raise 循环重复设回穿透）。"""
        super().showEvent(event)
        if getattr(self, "_native_passthrough_set", False):
            return  # 已经设过了，不重复
        self._native_passthrough_set = True
        try:
            import sys as _sys, objc
            if _sys.platform == "darwin":
                wid = int(self.winId())
                if wid:
                    ns_win = objc.objc_object(c_void_p=wid).window()
                    if ns_win is not None:
                        ns_win.setIgnoresMouseEvents_(True)
        except Exception:
            pass

    def set_tool(self, tool):
        """切换工具：None=穿透（不影响用户操作），rect/arrow=可绘制。"""
        self._tool = tool
        # 同时操作 Qt 层 + macOS native 层（NSWindow.setIgnoresMouseEvents）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, tool is None)
        try:
            import sys as _sys, objc
            if _sys.platform == "darwin":
                wid = int(self.winId())
                if wid:
                    ns_win = objc.objc_object(c_void_p=wid).window()
                    if ns_win is not None:
                        ns_win.setIgnoresMouseEvents_(tool is None)
        except Exception:
            pass
        if tool is not None:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._tool:
            self._drawing = True
            self._start = event.pos()
            self._current = event.pos()
            self._update_preview()

    def mouseMoveEvent(self, event):
        if self._drawing:
            self._current = event.pos()
            self._update_preview()
            self.update()  # 触发 paintEvent 实时预览

    def _update_preview(self):
        """把绘制中的标注推给 recorder（GIF 帧预览）+ 触发自身重绘（实时预览）。"""
        if not self._drawing or not self._tool:
            return
        color = (self._color.red(), self._color.green(), self._color.blue(), 255)
        if self._tool == "rect":
            x0, y0 = min(self._start.x(), self._current.x()), min(self._start.y(), self._current.y())
            x1, y1 = max(self._start.x(), self._current.x()), max(self._start.y(), self._current.y())
            self._recorder._preview_ann = {
                "tool": "rect", "rect": (x0, y0, x1, y1),
                "color": color, "width": 3,
            }
        elif self._tool == "arrow":
            self._recorder._preview_ann = {
                "tool": "arrow",
                "start_pos": (self._start.x(), self._start.y()),
                "end_pos": (self._current.x(), self._current.y()),
                "color": color, "width": 3,
            }

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drawing:
            self._drawing = False
            end = event.pos()
            # 清除预览
            self._recorder._preview_ann = None
            added = False
            if self._tool == "rect":
                x0, y0 = min(self._start.x(), end.x()), min(self._start.y(), end.y())
                x1, y1 = max(self._start.x(), end.x()), max(self._start.y(), end.y())
                if x1 - x0 > 3 and y1 - y0 > 3:
                    self._recorder.add_annotation({
                        "tool": "rect",
                        "rect": (x0, y0, x1, y1),
                        "color": (self._color.red(), self._color.green(), self._color.blue(), 255),
                        "width": 3,
                    })
                    added = True
            elif self._tool == "arrow":
                dx = end.x() - self._start.x()
                dy = end.y() - self._start.y()
                if dx * dx + dy * dy > 25:
                    self._recorder.add_annotation({
                        "tool": "arrow",
                        "start_pos": (self._start.x(), self._start.y()),
                        "end_pos": (end.x(), end.y()),
                        "color": (self._color.red(), self._color.green(), self._color.blue(), 255),
                        "width": 3,
                    })
                    added = True
            # 如果画了标注，通知 SelectionWindow 显示"闪烁中"提示
            if added and hasattr(self, "_on_ann_added") and self._on_ann_added:
                self._on_ann_added()
            self._tool = None  # 画完恢复穿透
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            # native 层也恢复穿透
            try:
                import sys as _sys, objc
                if _sys.platform == "darwin":
                    wid = int(self.winId())
                    if wid:
                        ns_win = objc.objc_object(c_void_p=wid).window()
                        if ns_win is not None:
                            ns_win.setIgnoresMouseEvents_(True)
            except Exception:
                pass
            self.update()

    def paintEvent(self, event):
        """绘制正在画中的标注预览。"""
        if not self._drawing or not self._tool:
            return
        painter = QPainter(self)
        pen = QPen(self._color, 3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._tool == "rect":
            painter.drawRect(QRect(self._start, self._current).normalized())
        elif self._tool == "arrow":
            painter.drawLine(self._start, self._current)
            # 简易箭头头
            import math
            angle = math.atan2(self._current.y() - self._start.y(),
                               self._current.x() - self._start.x())
            for da in [-0.5, 0.5]:
                hx = self._current.x() - 12 * math.cos(angle + da)
                hy = self._current.y() - 12 * math.sin(angle + da)
                painter.drawLine(self._current, QPoint(int(hx), int(hy)))


class SelectionWindow(QWidget):
    """截图选区窗口 - 沉浸式十字光标选区 + 标注"""
    finished = pyqtSignal()  # 窗口关闭信号

    def __init__(self):
        super().__init__()
        self.background_pixmap = None
        self.selection_rect = QRect()
        self.is_selecting = False
        self.start_point = QPoint()

        # 标注相关
        self.annotations = []
        # 工具/颜色/线宽持久化：从 QSettings 恢复上次选择
        self._settings = QSettings("Shorts", "Shorts")
        self.current_tool = self._settings.value("tool", "arrow", type=str)
        _saved_color = self._settings.value("color", "#ff3b30", type=str)
        _c = QColor(_saved_color)
        self.current_color = _c if _c.isValid() else QColor(255, 59, 48)
        _saved_w = self._settings.value("width", 2, type=int)
        self.current_width = _saved_w if _saved_w in (1, 2, 4) else 2
        # 文字字号独立于线宽(已解耦);从 QSettings 恢复,默认 14
        _saved_ts = self._settings.value("text_size", 14, type=int)
        self.current_text_size = _saved_ts if _saved_ts in (12, 16, 22) else 14
        # 马赛克网格大小(粗14/中10/细6,默认中10);块像素越大越粗,持久化
        _saved_ms = self._settings.value("mosaic_size", 10, type=int)
        self.current_mosaic_size = _saved_ms if _saved_ms in (6, 10, 14) else 10
        # 序号标注缩放(小0.85/中1.0/大1.25,默认中1.0);驱动圆半径/数字与文字字号,持久化
        _saved_ss = self._settings.value("seq_scale", 1.0, type=float)
        self.current_seq_scale = _saved_ss if _saved_ss in (0.85, 1.0, 1.25) else 1.0
        # 填充色(None=无填充);仅矩形/椭圆使用,不持久化,每次默认无填充
        self.current_fill_color = None
        self._FILL_ALPHA = 160  # 填充半透明度(0-255):兼顾高亮与下层可见
        self.is_drawing = False
        self.current_rect = None
        self.current_end_pos = None  # 当前绘制终点（用于箭头等）
        self.current_points = []  # 画笔路径点
        self.selection_confirmed = False  # 选区是否已确认

        # 序号标注相关
        self.sequence_number = 0  # 序号计数器

        # 滚动截图相关
        self.is_scroll_capturing = False
        self.scroll_frames = []          # 捕获的稳定帧列表 [QPixmap]
        self.scroll_capture_rect = None  # 捕获区域(屏幕坐标 QRect)
        self.scroll_timer = None         # QTimer
        self.scroll_no_change_count = 0  # 连续无变化采样数(用于自动结束)
        self.scroll_last_bytes = None    # 上一个已保存帧的像素字节(用于去重/对齐)
        self.scroll_last_sample = None   # 上一次"采样"的字节(未保存,用于稳定态判定)
        self.scroll_stable_count = 0     # 当前采样与上次采样相同的连续次数
        self.scroll_original_pixmap = None  # 拼接后的全分辨率长图
        self.scroll_scale_factor = 1.0   # 显示缩放因子(原始高/显示高)
        self.scroll_capture_bar = None   # 浮窗引用
        self.scroll_capture_outline = None  # 捕获区域边框

        # GIF 录制相关
        self._gif_rect = None
        self._gif_frames = []
        self._gif_recorder = None
        self._gif_bar = None
        self._gif_status = None
        self._gif_show_clicks = True
        self._gif_outline = None

        # 滚动结果视图的缩放/平移（仅滚动截图完成后启用；正常模式下为恒等变换）
        self.view_zoom = 1.0              # 显示层之上的额外缩放倍数
        self.view_offset = QPointF(0, 0)  # 平移偏移（屏幕像素）
        self.panning = False              # 中键拖动平移中
        self.pan_last = QPoint()          # 上一次平移光标位置

        # 悬停位置
        self.hover_pos = QPoint()

        # 窗口智能识别（选区未确认阶段）：鼠标悬停高亮光标下的整窗口，
        # 单击即选中该窗口为选区。dragging 时关闭。
        self.hover_window_rect = None
        self._window_click_pending = False
        self._window_press_pos = QPoint()
        self._window_click_rect = None
        self._window_query_timer = QTimer(self)
        self._window_query_timer.setSingleShot(True)
        self._window_query_timer.timeout.connect(self._query_hover_window)
        self._window_query_scheduled = False

        # 当前选中的标注(用于删除)。点击标注时设定，点击空白/新建标注时清除。
        self.selected_ann = None

        # 撤销/重做：基于 annotations 快照的"历史指针"模型。
        # history 存放每一提交点的 deepcopy 快照，hist_idx 指向当前状态。
        self._history = [[]]
        self._hist_idx = 0

        # 马赛克绘制缓存：按 (ann id, 区域, 背景版本) 缓存像素化后的 pixmap，
        # 避免每次重绘都 toImage() 全屏拷贝 + 逐像素取色。背景变化时
        # _bg_version 自增使旧缓存失效。
        self._mosaic_cache = {}
        self._bg_version = 0

        # 点击命中"重叠标注"时的循环选中：同一位置连续点击，依次切到下层标注，
        # 解决多个标注包围盒重叠时抓不到下面那个的问题。
        self._cycle_anchor = None   # 上次开始循环的点击位置(屏幕坐标)
        self._cycle_idx = 0         # 当前在候选列表里选中的序号

        # 设置窗口属性
        self.setWindowTitle("Shorts - 截图")
        # 选区遮罩：无边框置顶，但不用 Qt.Tool——Tool 窗口在 macOS 上不接收
        # 键盘焦点，会导致 Esc 无法取消截图。盖 Dock 的需求交给工具栏/样式面板
        # 的 raise_overlay（NSStatusWindowLevel）。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAutoFillBackground(False)

        # 多显示器：选区窗口覆盖整个虚拟桌面(所有屏幕的并集)
        self.setGeometry(_virtual_desktop_geometry())

        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setMouseTracking(True)

    def _handle_escape(self):
        """统一处理 Esc：取消文字输入 / 返回选择 / 取消截图。"""
        if self.is_scroll_capturing:
            self._finish_scroll_capture()
            return
        if hasattr(self, 'text_input') and self.text_input:
            self.text_input.deleteLater()
            self.text_input = None
            return
        if self.selection_confirmed:
            self.selection_confirmed = False
            self.selection_rect = QRect()
            self.annotations = []
            self.selected_ann = None
            self._reset_undo()
            if hasattr(self, 'toolbar'):
                self.toolbar.hide()
            if hasattr(self, 'style_panel'):
                self.style_panel.hide()
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            self.update()
        else:
            self.cancelled = True
            self.close()
            self.finished.emit()

    def closeEvent(self, event):
        # 兜底：选区窗口关闭时，确保所有浮层（工具栏/样式面板）一并消失。
        # 它们是独立顶层窗口，不会随父窗口自动关闭。
        self._close_overlays()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # macOS 无边框置顶窗口拿不到键盘焦点 → keyPressEvent 不触发。用 QShortcut
        # 绑定 Esc，并用 ApplicationShortcut 上下文：无论焦点在哪个子窗口/工具栏
        # （包括窗口截图单击后焦点漂移的情况），Esc 都能取消。
        if not getattr(self, '_esc_shortcut', None):
            from PyQt6.QtGui import QShortcut, QKeySequence
            self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
            self._esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self._esc_shortcut.activated.connect(self._handle_escape)

    def set_background(self, pixmap):
        """设置背景截图"""
        self.background_pixmap = pixmap
        self._bg_version += 1
        self._mosaic_cache.clear()
        self.update()

    def start_capture(self):
        """开始截图"""
        # macOS：截图前隐藏系统光标，避免光标残留在背景图里。
        # 截完后选区窗口会显示自己的 CrossCursor（setCursor 已设）。
        if is_macos():
            try:
                from Quartz import CGDisplayHideCursor, CGMainDisplayID
                CGDisplayHideCursor(CGMainDisplayID())
            except Exception:
                pass
        screenshot = Screenshot()
        # monitor_index=0 = mss 的"所有显示器合并"虚拟屏，配合多显示器选区窗口
        self.background_pixmap = screenshot.capture_full_screen(monitor_index=0)
        self._bg_version += 1
        self._mosaic_cache.clear()
        # 多显示器：必须用 show() 而非 showFullScreen()——后者会把无边框窗口
        # 限制在单个屏幕内，无法跨屏。setGeometry 已铺满整个虚拟桌面。
        self.show()
        self.raise_()
        self.activateWindow()
        # 选区窗口显示后恢复光标（显示自己的 CrossCursor）
        if is_macos():
            try:
                from Quartz import CGDisplayShowCursor, CGMainDisplayID
                CGDisplayShowCursor(CGMainDisplayID())
            except Exception:
                pass

    def _is_scroll_result(self):
        """当前是否处于"滚动截图结果"视图"""
        return bool(self.scroll_original_pixmap and not self.scroll_original_pixmap.isNull())

    def _img_pos(self, pos):
        """屏幕坐标 -> 显示层图像坐标。
        非滚动结果视图下原样返回(恒等)，确保正常选区/标注流程零影响。"""
        if not self._is_scroll_result():
            return QPoint(pos)
        return QPoint(
            int((pos.x() - self.view_offset.x()) / self.view_zoom),
            int((pos.y() - self.view_offset.y()) / self.view_zoom),
        )

    def _img_to_screen(self, pos):
        """显示层图像坐标 -> 屏幕坐标。"""
        if not self._is_scroll_result():
            return QPoint(pos)
        return QPoint(
            int(pos.x() * self.view_zoom + self.view_offset.x()),
            int(pos.y() * self.view_zoom + self.view_offset.y()),
        )

    # ---- 撤销/重做(annotations 快照历史) ----
    def _reset_undo(self):
        """清空历史，重新以"空标注列表"作为初始状态。"""
        self._history = [[]]
        self._hist_idx = 0

    def _commit_undo(self):
        """在标注发生改变后调用：把当前 annotations 深拷贝快照压入历史，
        丢弃 hist_idx 之后的 redo 分支。与上一快照相同则跳过(避免无操作产生的空撤销步)。"""
        snap = copy.deepcopy(self.annotations)
        self._history = self._history[:self._hist_idx + 1]
        if self._history and self._history[-1] == snap:
            return
        self._history.append(snap)
        self._hist_idx = len(self._history) - 1

    def _undo(self):
        if self._hist_idx <= 0:
            return
        self._hist_idx -= 1
        self.annotations = copy.deepcopy(self._history[self._hist_idx])
        self.selected_ann = None
        self._mosaic_cache.clear()  # deepcopy 产生新 ann id，旧马赛克缓存失效
        self.update()

    def _redo(self):
        if self._hist_idx >= len(self._history) - 1:
            return
        self._hist_idx += 1
        self.annotations = copy.deepcopy(self._history[self._hist_idx])
        self.selected_ann = None
        self._mosaic_cache.clear()
        self.update()

    # ---- 选中标注的包围盒与高亮 ----
    def _annotation_bbox(self, ann):
        """计算单个标注的包围矩形(标注自身坐标系，不含绘制偏移)。"""
        tool = ann.get("tool", "")
        if tool == "arrow":
            s, e = ann.get("start_pos"), ann.get("end_pos")
            if s and e:
                return QRect(QPoint(min(s.x(), e.x()), min(s.y(), e.y())),
                             QPoint(max(s.x(), e.x()), max(s.y(), e.y())))
        if tool == "pen":
            pts = ann.get("points") or []
            if pts:
                xs = [p.x() for p in pts]
                ys = [p.y() for p in pts]
                return QRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if tool == "sequence":
            cp = ann.get("circle_pos")
            r = ann.get("circle_radius", 12)
            tp = ann.get("text_pos")
            parts = []
            if cp:
                parts.append(QRect(cp.x() - r, cp.y() - r, r * 2, r * 2))
            if tp:
                parts.append(QRect(tp.x(), tp.y(), 100, 20))
            if parts:
                box = QRect(parts[0])
                for p in parts[1:]:
                    box = box.united(p)
                return box
        rect = ann.get("rect")
        if rect:
            return QRect(rect)
        return QRect()

    def _draw_selection_highlight(self, painter):
        """在选中的标注周围画蓝色虚线框(绘制偏移由调用方的变换负责)。"""
        sel = getattr(self, 'selected_ann', None)
        if not sel:
            return
        # 仅当仍是当前列表中的对象时才高亮(避免 undo/删除后残留)
        if not any(a is sel for a in self.annotations):
            return
        bbox = self._annotation_bbox(sel)
        if not bbox.isValid():
            return
        pad = 4
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor("#0a84ff"), 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(bbox.adjusted(-pad, -pad, pad, pad))

    def _draw_sequence_editing(self, painter):
        """绘制"正在输入"的序号标注：圆圈、连线、文字边框（文字由 QLineEdit 原生渲染）。
        调用方负责坐标系：正常模式为屏幕坐标，滚动结果模式为图像坐标(已施加变换)。"""
        if not (hasattr(self, 'current_sequence_ann') and self.current_sequence_ann):
            return
        ann = self.current_sequence_ann
        circle_pos = ann.get("circle_pos")
        text_pos = ann.get("text_pos")
        number = ann.get("number", 1)
        _seq_scale = ann.get("seq_scale", 1.0)
        radius, _num_pt, _text_pt, text_h, _pad_x = self._seq_metrics(_seq_scale)
        color = ann.get("color")
        if color is None:
            color = QColor(255, 59, 48)

        if circle_pos and text_pos:
            text_center_y = text_pos.y() + text_h / 2
            painter.setPen(QPen(color, 2))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawLine(int(circle_pos.x()), int(circle_pos.y()), int(text_pos.x()), int(text_center_y))

        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        rect_circle = QRect(
            int(circle_pos.x() - radius),
            int(circle_pos.y() - radius),
            radius * 2,
            radius * 2
        )
        painter.drawEllipse(rect_circle)
        painter.setPen(QPen(Qt.GlobalColor.white))
        painter.setFont(QFont("Arial", _num_pt, QFont.Weight.Bold))
        painter.drawText(rect_circle, int(Qt.AlignmentFlag.AlignCenter), str(number))

        if text_pos:
            fm = QFontMetrics(QFont("Microsoft YaHei", _text_pt))
            text_width = max(100, fm.horizontalAdvance(ann.get("text", "")) + _pad_x * 2)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.setPen(QPen(color, 1))
            painter.drawRect(int(text_pos.x()), int(text_pos.y()), text_width, text_h)

    def _paint_scroll_result(self, painter):
        """滚动截图结果的缩放/平移视图。"""
        # 黑色底（缩放/平移露出时填充）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 20, 20))
        painter.drawRect(self.rect())

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.translate(self.view_offset)
        painter.scale(self.view_zoom, self.view_zoom)

        # 背景图：直接绘制全分辨率原图(按"显示层"逻辑尺寸)。
        # 旧实现绘制的是已缩放到屏幕高度的 background_pixmap，放大后会出现明显锯齿/模糊；
        # 这里改用全分辨率 scroll_original_pixmap，让 Qt 在缩放变换下从原图采样，放大依然清晰。
        # 标注使用"显示层"坐标，原图经 scroll_scale_factor 与显示层对应，保持对齐。
        if self.scroll_original_pixmap and not self.scroll_original_pixmap.isNull():
            sf = self.scroll_scale_factor if self.scroll_scale_factor > 0 else 1.0
            ow = self.scroll_original_pixmap.width()
            oh = self.scroll_original_pixmap.height()
            painter.drawPixmap(QRectF(0, 0, ow / sf, oh / sf),
                               self.scroll_original_pixmap, QRectF(0, 0, ow, oh))

        # 已保存标注（跳过正在输入的序号标注）
        for ann in self.annotations:
            if ann is getattr(self, 'current_sequence_ann', None):
                continue
            self._draw_annotation(painter, ann, 0, 0)

        # 正在绘制的标注
        if self.current_rect and self.is_drawing:
            cur = {
                "tool": self.current_tool,
                "rect": QRect(self.current_rect),
                "color": QColor(self.current_color),
                "fill_color": QColor(self.current_fill_color) if self.current_fill_color else None,
                "width": self.current_width,
                "mosaic_size": self.current_mosaic_size,
            }
            if self.current_tool in ("arrow", "line"):
                cur["start_pos"] = QPoint(self.start_point)
                cur["end_pos"] = QPoint(self.current_end_pos) if self.current_end_pos else QPoint(self.current_rect.bottomRight())
                if self.current_tool == "line":
                    cur["dashed"] = bool(getattr(self, "current_line_dashed", False))
            if self.current_tool == "pen" and hasattr(self, 'current_points'):
                cur["points"] = list(self.current_points)
            self._draw_annotation(painter, cur, 0, 0)

        # 序号标注输入中
        self._draw_sequence_editing(painter)

        # 选中标注高亮(处于缩放/平移变换下)
        self._draw_selection_highlight(painter)

        painter.restore()

        # 缩放比例提示（屏幕坐标，不受变换影响）
        painter.setPen(QPen(QColor(255, 255, 255, 220)))
        painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
        painter.drawRoundedRect(QRect(12, 12, 60, 22), 6, 6)
        painter.drawText(QRect(12, 12, 60, 22), Qt.AlignmentFlag.AlignCenter,
                         f"{int(round(self.view_zoom * 100))}%")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 滚动截图结果：走独立的缩放/平移视图，不复用下方选区遮罩逻辑
        if self._is_scroll_result():
            self._paint_scroll_result(painter)
            return

        if self.selection_rect.isValid():
            # 有选区：绘制遮罩，然后在其上绘制选区内容

            # 1. 绘制整个背景截图
            if self.background_pixmap:
                painter.drawPixmap(0, 0, self.background_pixmap)

            # 2. 绘制非选区区域的遮罩（统一颜色，不重叠）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 220))  # 统一深色遮罩

            # 左侧（选区高度范围内的左侧）
            if self.selection_rect.left() > 0:
                painter.drawRect(0, self.selection_rect.top(), self.selection_rect.left(), self.selection_rect.height())
            # 右侧（选区高度范围内的右侧）
            if self.selection_rect.right() < self.rect().width():
                painter.drawRect(self.selection_rect.right(), self.selection_rect.top(), self.rect().width() - self.selection_rect.right(), self.selection_rect.height())
            # 顶部（选区上方的整个宽度）
            if self.selection_rect.top() > 0:
                painter.drawRect(0, 0, self.rect().width(), self.selection_rect.top())
            # 底部（选区下方的整个宽度）
            if self.selection_rect.bottom() < self.rect().height():
                painter.drawRect(0, self.selection_rect.bottom(), self.rect().width(), self.rect().height() - self.selection_rect.bottom())

            # 3. 绘制选区边框 (白色细线)
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(self.selection_rect)

            # 3.5 绘制选区控制柄
            handles = self._get_rect_handles(self.selection_rect)
            painter.setBrush(QBrush(Qt.GlobalColor.white))
            painter.setPen(QPen(QColor(0, 120, 215), 1))
            for handle_pos in handles.values():
                painter.drawRect(int(handle_pos.x()) - 4, int(handle_pos.y()) - 4, 8, 8)

            # 4. 绘制已保存的标注（跳过正在输入的序号标注，避免重复绘制）
            for ann in self.annotations:
                if ann is getattr(self, 'current_sequence_ann', None):
                    continue  # 跳过正在输入的序号标注，其文字由第6步绘制
                self._draw_annotation(painter, ann)

            # 5. 绘制当前正在画的标注
            if self.current_rect and self.is_drawing:
                ann = {
                    "tool": self.current_tool,
                    "rect": QRect(self.current_rect),
                    "color": self.current_color,
                    "width": self.current_width
                }
                # 箭头绘制时添加起点和终点
                if self.current_tool == "arrow":
                    ann["start_pos"] = QPoint(self.start_point)
                    ann["end_pos"] = QPoint(self.current_end_pos) if self.current_end_pos else QPoint(self.current_rect.bottomRight())
                self._draw_annotation(painter, ann)

            # 6. 绘制控制柄（悬停时显示）
            hovered_ann = None
            if hasattr(self, 'hover_pos') and self.selection_rect.contains(self.hover_pos):
                hit = self._hit_test_annotation(self.hover_pos)
                if hit:
                    ann, hit_type = hit
                    if ann.get("tool") in ("rectangle", "ellipse"):
                        hovered_ann = ann
            # 正在拖动控制柄时也显示
            if hasattr(self, 'dragging_ann') and self.dragging_ann and self.dragging_type.startswith("handle_"):
                hovered_ann = self.dragging_ann

            if hovered_ann:
                handles = self._get_rect_handles(hovered_ann["rect"])
                painter.setBrush(QBrush(Qt.GlobalColor.white))
                painter.setPen(QPen(QColor(0, 120, 215), 1))
                for handle_pos in handles.values():
                    painter.drawRect(int(handle_pos.x()) - 4, int(handle_pos.y()) - 4, 8, 8)

            # 6. 序号标注正在输入时，绘制序号圆圈、连线和文字边框（统一走 _draw_sequence_editing，支持 seq_scale 大小档）
            self._draw_sequence_editing(painter)

            # 8. 文字标注已由第4步 _draw_annotation 统一绘制（AlignVCenter，与编辑时一致）

            # 9. 选中标注高亮(删除目标提示)
            self._draw_selection_highlight(painter)
        else:
            # 没有选区：显示完整背景截图
            if self.background_pixmap:
                painter.drawPixmap(0, 0, self.background_pixmap)

            # 窗口智能识别：若悬停在某个窗口上，绘制该窗口的高亮边框
            if self.hover_window_rect and self.hover_window_rect.isValid():
                painter.setPen(QPen(QColor(0, 120, 215), 2))
                painter.setBrush(QBrush(QColor(0, 120, 215, 40)))
                painter.drawRect(self.hover_window_rect)

            # 绘制尺寸标签
            w = self.selection_rect.width()
            h = self.selection_rect.height()
            text = f"{w} × {h}"

            painter.setPen(QPen(QColor(0, 0, 0, 200)))
            painter.setBrush(QBrush(QColor(0, 0, 0, 200)))
            label_rect = QRect(
                self.selection_rect.x(),
                self.selection_rect.bottom() + 8,
                90, 24
            )
            painter.drawRoundedRect(label_rect, 4, 4)
            painter.setPen(QPen(Qt.GlobalColor.white))
            font = QFont()
            font.setPixelSize(12)
            painter.setFont(font)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, text)

    def _hit_test_selection_handle(self, pos, handle_radius=8, edge_threshold=3):
        """检测点击是否在选区控制柄上，返回控制柄类型或None"""
        rect = self.selection_rect
        handles = self._get_rect_handles(rect)

        # 优先检测角落控制柄（精确匹配）
        corner_handles = ["handle_tl", "handle_tr", "handle_br", "handle_bl"]
        for handle_type in corner_handles:
            handle_pos = handles[handle_type]
            if abs(pos.x() - handle_pos.x()) <= handle_radius and abs(pos.y() - handle_pos.y()) <= handle_radius:
                return handle_type

        # 检测边的控制柄
        edge_handles = {
            "handle_t": handles["handle_t"],
            "handle_b": handles["handle_b"],
            "handle_l": handles["handle_l"],
            "handle_r": handles["handle_r"],
        }
        for handle_type, handle_pos in edge_handles.items():
            if abs(pos.x() - handle_pos.x()) <= handle_radius and abs(pos.y() - handle_pos.y()) <= handle_radius:
                return handle_type

        # 检测是否在边框上（移动整个选区）- 只在边缘区域检测
        x, y = pos.x(), pos.y()
        left, right, top, bottom = rect.left(), rect.right(), rect.top(), rect.bottom()

        # 左边框
        if abs(x - left) <= edge_threshold and top < y < bottom:
            return "move"
        # 右边框
        if abs(x - right) <= edge_threshold and top < y < bottom:
            return "move"
        # 上边框
        if abs(y - top) <= edge_threshold and left < x < right:
            return "move"
        # 下边框
        if abs(y - bottom) <= edge_threshold and left < x < right:
            return "move"

        return None

    def mousePressEvent(self, event):
        # 中键：滚动结果视图下平移
        if event.button() == Qt.MouseButton.MiddleButton and self._is_scroll_result():
            # 收起进行中的文字输入，避免输入框位置/字号错位
            if getattr(self, 'text_input', None):
                self._finish_text_input()
            self.panning = True
            self.pan_last = event.pos()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # 拖动(平移)工具：左键拖动平移图像——触摸板无中键时的平移入口。
            # 复用中键平移的 self.panning/pan_last 与 move/release 处理(零新状态)；
            # 仅在滚动结果视图(有缩放/偏移)下生效，其它视图无缩放意义，直接忽略。
            if self.current_tool == "pan":
                if self._is_scroll_result():
                    if getattr(self, 'text_input', None):
                        self._finish_text_input()
                    self.panning = True
                    self.pan_last = event.pos()
                    self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                return
            pos = self._img_pos(event.pos())
            # 默认清除选中；若点中标注，_start_dragging 会重新选中它
            self.selected_ann = None
            if self.selection_confirmed:
                # 取色器：点击从背景图吸色，设为当前标注色（连续吸色，不切走）
                if self.current_tool == "picker":
                    self._pick_color_at(pos)
                    return
                # 选区控制柄/边框位于选区边界上，其可点击区域（命中半径 8px）
                # 会延伸到 selection_rect.contains() 之外。Qt 的 QRect 是
                # [left, right] 闭区间（right = left + width - 1），而控制柄
                # 右/下侧位置由 left+width 计算落在 right+1 处，导致 contains()
                # 为 False。因此控制柄命中检测必须在 contains 门控之前进行，
                # 否则右侧/下侧的控制柄（tr/r/br/b/bl）会拖不动。
                # （滚动结果视图选区即整张图，不交互选区控制柄）
                sel_handle = None if self._is_scroll_result() else self._hit_test_selection_handle(pos)
                if sel_handle:
                    # 先完成之前的输入
                    if hasattr(self, 'text_input') and self.text_input:
                        self._finish_text_input()
                    self._start_selection_drag(sel_handle, pos)
                    return

                if self.selection_rect.contains(pos):
                    # 先完成之前的输入
                    if hasattr(self, 'text_input') and self.text_input:
                        self._finish_text_input()

                    # 命中所有重叠标注(从上到下)。同一位置连续点击会循环切到下层，
                    # 解决多个标注包围盒重叠时抓不到下面那个的问题。
                    candidates = self._hit_test_annotations(pos)
                    if candidates:
                        ann, hit_type = self._pick_candidate(pos, candidates)
                        self._start_dragging(ann, hit_type, pos)
                        return
                    else:
                        # 点到空白：重置循环锚点
                        self._cycle_anchor = None

                    # 点击在空白区域，根据当前工具创建新标注
                    if self.current_tool == "text":
                        self._handle_text_annotation(pos)
                    elif self.current_tool == "sequence":
                        self._add_sequence_annotation(pos)
                    elif self.current_tool == "pen":
                        self.is_drawing = True
                        self.start_point = pos
                        self.current_points = [pos]
                        self.current_rect = QRect(pos, pos)
                    elif self.current_tool in ("rectangle", "ellipse", "arrow", "line", "highlight", "mosaic"):
                        self.is_drawing = True
                        self.start_point = pos
                        self.current_end_pos = pos
                        self.current_rect = QRect(self.start_point, self.start_point)
            else:
                self.is_selecting = True
                self.is_drawing = False
                self.start_point = pos
                self.selection_rect = QRect(self.start_point, self.start_point)
                # 窗口智能选中：若按下时正悬停在某个窗口上，记录候选；拖动超阈值则放弃
                self._window_press_pos = pos
                self._window_click_rect = (
                    QRect(self.hover_window_rect) if self.hover_window_rect else None
                )
            self.update()

    def _get_rect_handles(self, rect):
        """获取矩形的8个控制柄位置和类型"""
        x, y = rect.x(), rect.y()
        w, h = rect.width(), rect.height()
        handles = {
            "handle_tl": QPoint(x, y),
            "handle_t": QPoint(x + w // 2, y),
            "handle_tr": QPoint(x + w, y),
            "handle_r": QPoint(x + w, y + h // 2),
            "handle_br": QPoint(x + w, y + h),
            "handle_b": QPoint(x + w // 2, y + h),
            "handle_bl": QPoint(x, y + h),
            "handle_l": QPoint(x, y + h // 2),
        }
        return handles

    def _hit_test_handle(self, pos, rect, handle_radius=5):
        """检测点击是否在控制柄上，返回控制柄类型或None"""
        handles = self._get_rect_handles(rect)
        for handle_type, handle_pos in handles.items():
            if abs(pos.x() - handle_pos.x()) <= handle_radius and abs(pos.y() - handle_pos.y()) <= handle_radius:
                return handle_type
        return None

    @staticmethod
    def _dist_to_segment(p, a, b):
        """点 p 到线段 a-b 的欧氏距离。用于箭头/画笔的"沿线命中"。"""
        px, py = p.x(), p.y()
        ax, ay = a.x(), a.y()
        bx, by = b.x(), b.y()
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        cx, cy = ax + t * dx, ay + t * dy
        return math.hypot(px - cx, py - cy)

    def _hit_test_annotations(self, pos):
        """返回 pos 处所有可命中的标注，按"从上到下"(绘制顺序倒序)排列，
        每项为 (ann, hit_type)。

        与旧的"包围盒 contains"不同：
        - 箭头按"线段"命中(点到线段距离)，不再用整个包围盒挡住下层标注；
        - 画笔按"折线段"命中；
        - 矩形/椭圆/文字/高亮/马赛克按其矩形区域命中。
        重叠时由调用方(mousePressEvent)配合"同点循环点击"依次切换下层。
        """
        hits = []
        for i in range(len(self.annotations) - 1, -1, -1):
            ann = self.annotations[i]
            tool = ann.get("tool", "")

            if tool == "text":
                rect = ann.get("rect")
                if rect and rect.contains(pos):
                    # 检测点击位置：边框区域 = 拖动，中间 = 编辑
                    border_w = 4
                    is_border = (
                        pos.x() <= rect.x() + border_w or
                        pos.x() >= rect.right() - border_w or
                        pos.y() <= rect.y() + border_w or
                        pos.y() >= rect.bottom() - border_w
                    )
                    hits.append((ann, "text_border" if is_border else "text_edit"))

            elif tool == "rectangle" or tool == "ellipse":
                rect = ann.get("rect")
                if rect and rect.contains(pos):
                    # 先检测是否在控制柄上（控制柄优先级最高）
                    handle = self._hit_test_handle(pos, rect)
                    if handle:
                        hits.append((ann, handle))  # 如 "handle_tl", "handle_br"
                        continue
                    # 检测是否在边框区域（移动）
                    border_w = 4
                    is_border = (
                        pos.x() <= rect.x() + border_w or
                        pos.x() >= rect.right() - border_w or
                        pos.y() <= rect.y() + border_w or
                        pos.y() >= rect.bottom() - border_w
                    )
                    hits.append((ann, "rect_border" if is_border else "rect"))

            elif tool == "arrow" or tool == "line":
                # 沿线段命中(而非整个包围盒)，避免大包围盒挡住下层标注
                start = ann.get("start_pos")
                end = ann.get("end_pos")
                if start and end:
                    # 端点附近优先(可拖动端点调整大小)
                    near_point = False
                    for p in [start, end]:
                        if (pos.x() - p.x()) ** 2 + (pos.y() - p.y()) ** 2 <= 15 * 15:
                            near_point = True
                            break
                    if near_point:
                        hits.append((ann, "point"))
                    else:
                        w = ann.get("width", 4)
                        tol = max(6.0, w / 2 + 3)
                        if self._dist_to_segment(pos, start, end) <= tol:
                            hits.append((ann, "arrow"))

            elif tool == "pen":
                # 沿画笔折线命中
                pts = ann.get("points")
                w = ann.get("width", 4)
                tol = max(6.0, w / 2 + 3)
                got = False
                if pts and len(pts) >= 2:
                    for k in range(len(pts) - 1):
                        if self._dist_to_segment(pos, pts[k], pts[k + 1]) <= tol:
                            got = True
                            break
                elif pts and len(pts) == 1:
                    if (pos.x() - pts[0].x()) ** 2 + (pos.y() - pts[0].y()) ** 2 <= tol * tol:
                        got = True
                if got:
                    hits.append((ann, "pen"))

            elif tool == "highlight" or tool == "mosaic":
                # 面状标注：按矩形区域命中(可整体移动)
                rect = ann.get("rect")
                if rect and rect.contains(pos):
                    hits.append((ann, tool))

            elif tool == "sequence":
                # 序号标注：检测圆圈和文字区域
                circle_pos = ann.get("circle_pos")
                radius = ann.get("circle_radius", 12)
                if circle_pos:
                    dx = pos.x() - circle_pos.x()
                    dy = pos.y() - circle_pos.y()
                    if dx * dx + dy * dy <= radius * radius:
                        hits.append((ann, "sequence_circle"))
                        continue

                text_pos = ann.get("text_pos")
                text = ann.get("text", "")
                if text_pos:
                    _r, _n, _tp, _th, _pad_x = self._seq_metrics(ann.get("seq_scale", 1.0))
                    fm = QFontMetrics(QFont("Microsoft YaHei", _tp))
                    text_width = max(100, fm.horizontalAdvance(text) + _pad_x * 2) if text else 100
                    text_rect = QRect(text_pos.x(), text_pos.y(), text_width, _th)
                    if text_rect.contains(pos):
                        hits.append((ann, "sequence_text"))

        return hits

    def _hit_test_annotation(self, pos):
        """单标注命中(取最上层一个)，供光标提示等使用。
        实际选择/移动走 _hit_test_annotations + 同点循环(见 mousePressEvent)。"""
        hits = self._hit_test_annotations(pos)
        return hits[0] if hits else None

    def _pick_candidate(self, pos, candidates):
        """从命中的候选列表(从上到下)里挑一个。

        同一位置(曼哈顿距离≤6px)连续点击时，循环切到下一层标注；点击移到别处
        则重新从最上层开始。这样多个标注包围盒重叠时，连点就能依次抓到下面那个。
        """
        same_spot = (
            self._cycle_anchor is not None
            and (pos - self._cycle_anchor).manhattanLength() <= 6
        )
        if same_spot:
            self._cycle_idx = (self._cycle_idx + 1) % len(candidates)
        else:
            self._cycle_idx = 0
            self._cycle_anchor = QPoint(pos)
        return candidates[self._cycle_idx]

    def _hit_test_sequence(self, pos):
        """检测点击是否在序号标注上，返回(annotation, hit_type)"""
        for i in range(len(self.annotations) - 1, -1, -1):
            ann = self.annotations[i]
            if ann.get("tool") != "sequence":
                continue
            _radius, _num_pt, _text_pt, _text_h, _pad_x = self._seq_metrics(ann.get("seq_scale", 1.0))

            # 检查圆圈
            circle_pos = ann.get("circle_pos")
            if circle_pos:
                dx = pos.x() - circle_pos.x()
                dy = pos.y() - circle_pos.y()
                if dx * dx + dy * dy <= _radius * _radius:
                    return (ann, "circle")

            # 检查文字区域
            text_pos = ann.get("text_pos")
            text = ann.get("text", "")
            if text_pos:
                fm = QFontMetrics(QFont("Microsoft YaHei", _text_pt))
                text_width = max(100, fm.horizontalAdvance(text) + _pad_x * 2) if text else 100
                text_rect = QRect(text_pos.x(), text_pos.y(), text_width, _text_h)
                if text_rect.contains(pos):
                    return (ann, "text")

        return None

    def _edit_text_annotation(self, ann, hit_info):
        """编辑已有文字标注"""
        if ann in self.annotations:
            self.annotations.remove(ann)
        # 保留原标注的颜色和字号，实现真正的所见即所得编辑
        old_color = QColor(ann.get("color", self.current_color))
        old_font_size = ann.get("width", self.current_text_size)  # text 标注 width 字段即字号
        old_text = ann.get("text", "")
        # 临时设置当前颜色/字号以匹配原标注(_handle_text_annotation 用 current_text_size 和 current_color)
        saved_color = self.current_color
        saved_ts = self.current_text_size
        self.current_color = old_color
        self.current_text_size = old_font_size
        self._handle_text_annotation(ann["rect"].topLeft())
        # 预填充原文字
        if self.text_input and old_text:
            self.text_input.setText(old_text)
        # 恢复
        self.current_color = saved_color
        self.current_text_size = saved_ts

    def _edit_sequence_text(self, ann):
        """编辑序号标注的文字"""
        self._show_sequence_input_inline(ann)

    def _start_dragging(self, ann, drag_type, pos):
        """开始拖动标注"""
        self.dragging_ann = ann
        self.selected_ann = ann  # 选中该标注(可按 Delete 删除)
        self.dragging_type = drag_type
        self.drag_start_pos = pos

        # 记录初始位置
        if drag_type == "sequence_circle":
            self.drag_initial_pos = QPoint(ann["circle_pos"].x(), ann["circle_pos"].y())
        elif drag_type == "sequence_text":
            self.drag_initial_pos = QPoint(ann["text_pos"].x(), ann["text_pos"].y())
        elif drag_type == "arrow":
            self.drag_initial_start = QPoint(ann["start_pos"].x(), ann["start_pos"].y())
            self.drag_initial_end = QPoint(ann["end_pos"].x(), ann["end_pos"].y())
        elif drag_type == "point":
            # 判断点击的是哪个端点
            start = ann["start_pos"]
            end = ann["end_pos"]
            dist_to_start = (pos.x() - start.x()) ** 2 + (pos.y() - start.y()) ** 2
            dist_to_end = (pos.x() - end.x()) ** 2 + (pos.y() - end.y()) ** 2
            self.drag_point_type = "start" if dist_to_start < dist_to_end else "end"
            self.drag_initial_start = QPoint(start.x(), start.y())
            self.drag_initial_end = QPoint(end.x(), end.y())
        elif drag_type == "rect_border" or drag_type == "rect" or drag_type == "ellipse" or drag_type == "text_border":
            rect = ann.get("rect")
            if rect:
                self.drag_initial_pos = QPoint(rect.x(), rect.y())
        elif drag_type in ("pen", "highlight", "mosaic"):
            rect = ann.get("rect")
            if rect:
                self.drag_initial_pos = QPoint(rect.x(), rect.y())
            if drag_type == "pen":
                # 画笔需整体平移所有路径点
                self.drag_initial_points = [QPoint(p) for p in ann.get("points", [])]
        elif drag_type.startswith("handle_"):
            # 控制柄调整大小
            rect = ann.get("rect")
            if rect:
                self.drag_initial_rect = QRect(rect)
                self.drag_handle_type = drag_type
        self.update()

    def _start_selection_drag(self, handle_type, pos):
        """开始拖动选区控制柄"""
        # 清除所有拖动状态
        self.dragging_ann = None
        self.dragging_type = None
        self.selection_dragging = False
        # 开始新的选区拖动
        self.selection_dragging = True
        self.selection_drag_type = handle_type
        self.selection_drag_start = pos
        self.selection_initial_rect = QRect(self.selection_rect)
        self.update()

    def _update_cursor(self, pos):
        """根据鼠标位置更新光标状态"""
        # 拖动(平移)工具：恒为抓手(拖动中握拳)，优先级最高
        if self.current_tool == "pan":
            self.setCursor(QCursor(
                Qt.CursorShape.ClosedHandCursor if self.panning
                else Qt.CursorShape.OpenHandCursor))
            return

        if not self.selection_confirmed:
            return

        # 先检查是否在选区控制柄上（滚动结果视图不交互选区控制柄）
        if not self._is_scroll_result():
            sel_hit = self._hit_test_selection_handle(pos)
            if sel_hit:
                if sel_hit.startswith("handle_"):
                    if sel_hit in ("handle_tl", "handle_br"):
                        self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
                    elif sel_hit in ("handle_tr", "handle_bl"):
                        self.setCursor(QCursor(Qt.CursorShape.SizeBDiagCursor))
                    elif sel_hit in ("handle_t", "handle_b"):
                        self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
                    elif sel_hit in ("handle_l", "handle_r"):
                        self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
                    return
                elif sel_hit == "move":
                    self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
                    return

        # 再检查是否在标注上
        hit = self._hit_test_annotation(pos)
        if hit:
            ann, hit_type = hit
            if hit_type in ("sequence_circle", "sequence_text", "text_border", "arrow", "rect_border", "point", "rect", "ellipse"):
                self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
                return
            # 控制柄光标
            if hit_type == "handle_tl" or hit_type == "handle_br":
                self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
                return
            if hit_type == "handle_tr" or hit_type == "handle_bl":
                self.setCursor(QCursor(Qt.CursorShape.SizeBDiagCursor))
                return
            if hit_type == "handle_t" or hit_type == "handle_b":
                self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
                return
            if hit_type == "handle_l" or hit_type == "handle_r":
                self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
                return

        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def _handle_text_annotation(self, pos):
        """处理文字标注 - 所见即所得"""
        # 先完成之前的输入
        if hasattr(self, 'text_input') and self.text_input:
            self._finish_text_input()

        # 使用与最终渲染相同的字体大小（图像坐标系下的字号）
        font_size = self.current_text_size

        # 缩放因子：滚动结果视图下 QLineEdit 的屏幕字号需放大，以匹配缩放后的绘制
        zoom = self.view_zoom if self._is_scroll_result() else 1.0
        screen_font_size = max(1, int(font_size * zoom))
        screen_pos = self._img_to_screen(pos)

        # 创建输入框（文字由 QLineEdit 原生渲染，保证光标与文字对齐）
        self.text_input = QLineEdit(self)
        font = QFont("Microsoft YaHei", screen_font_size)
        fm = QFontMetrics(font)
        text_h = fm.ascent() + fm.descent()
        self.text_input.setFixedSize(int(200 * zoom), text_h)
        self.text_input.move(screen_pos.x(), screen_pos.y())
        self.text_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: transparent;
                color: {self.current_color.name()};
                border: none;
                padding: 0px;
                selection-background-color: rgba(0, 120, 215, 180);
            }}
        """)
        self.text_input.setFont(font)
        self.text_input.setTextMargins(0, 0, 0, 0)
        self.text_input.setFrame(False)
        # 存储的是图像坐标，标注 rect 以此构建
        self.text_input_pos = QPoint(pos.x(), pos.y())
        self.text_input.show()
        self.text_input.setFocus()

        # 响应文本变化，触发重绘（显示输入中的文字）
        self.text_input.textChanged.connect(lambda: self.update())

        # 安装事件过滤器处理回车键
        self.text_input.installEventFilter(self)

    def _show_sequence_input_inline(self, ann):
        """为序号标注显示内联输入框（所见即所得）"""
        text_pos = ann.get("text_pos")
        if not text_pos:
            return

        # 先完成之前的输入
        if hasattr(self, 'text_input') and self.text_input:
            self._finish_text_input()

        # 记录当前正在编辑的序号标注
        self.current_sequence_ann = ann
        self.text_input_pos = QPoint(text_pos.x(), text_pos.y())

        # 根据已有文字长度设置初始宽度（与 paintEvent 边框同公式）
        zoom = self.view_zoom if self._is_scroll_result() else 1.0
        screen_pos = self._img_to_screen(text_pos)
        existing_text = ann.get("text", "")
        _r, _num_pt, _text_pt, _text_h, pad_x = self._seq_metrics(ann.get("seq_scale", 1.0))
        seq_font = QFont("Microsoft YaHei", max(1, int(_text_pt * zoom)))
        fm = QFontMetrics(seq_font)
        pad_x_z = max(1, int(pad_x * zoom))
        initial_width = max(100, fm.horizontalAdvance(existing_text) + pad_x_z * 2)
        input_h = max(10, int(_text_h * zoom))

        # 输入框文字由 QLineEdit 原生渲染（与圆圈同色），背景透明
        seq_color = ann.get("color") or QColor(255, 59, 48)
        self.text_input = QLineEdit(self)
        self.text_input.setFixedSize(initial_width, input_h)
        self.text_input.move(screen_pos.x(), screen_pos.y())
        self.text_input.setText(existing_text)
        self.text_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: transparent;
                color: {seq_color.name()};
                border: none;
                padding: 0px;
                selection-background-color: rgba(0, 120, 215, 180);
            }}
        """)
        self.text_input.setFont(seq_font)
        self.text_input.setTextMargins(pad_x_z, 0, pad_x_z, 0)
        self.text_input.setFrame(False)
        self.text_input.show()
        self.text_input.setFocus()

        # 响应文本变化，实时更新标注和调整宽度
        self.text_input.textChanged.connect(lambda text: self._on_sequence_text_changed(text))

        # 安装事件过滤器处理回车键
        self.text_input.installEventFilter(self)

    def _on_sequence_text_changed(self, text):
        """序号文字变化时更新标注"""
        if hasattr(self, 'current_sequence_ann') and self.current_sequence_ann:
            self.current_sequence_ann["text"] = text
            # 调整输入框宽度适应文字
            if hasattr(self, 'text_input') and self.text_input:
                f = self.text_input.font()
                fm = QFontMetrics(f)
                _pad_x_z = max(1, round(f.pointSize() * 0.6))
                width = max(100, fm.horizontalAdvance(text) + _pad_x_z * 2)
                self.text_input.setFixedWidth(width)
            self.update()

    def eventFilter(self, obj, event):
        """事件过滤器 - 处理文字输入框的回车键（Esc 由 QShortcut 处理）。"""
        if obj == getattr(self, 'text_input', None):
            if event.type() == event.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    QTimer.singleShot(0, self._finish_text_input)
                    return True
        return super().eventFilter(obj, event)

    def _finish_text_input(self):
        """完成文字输入 - 通用版本"""
        if not hasattr(self, 'text_input') or not self.text_input:
            return

        # 检查是否是序号标注的输入
        if hasattr(self, 'current_sequence_ann') and self.current_sequence_ann:
            self._finish_sequence_input()
            return

        text = self.text_input.text().strip()
        if text:
            font_size = self.current_text_size
            font = QFont("Microsoft YaHei", font_size)
            fm = QFontMetrics(font)
            text_width = fm.horizontalAdvance(text) + 2
            text_height = fm.ascent() + fm.descent()
            self.annotations.append({
                "tool": "text",
                "rect": QRect(self.text_input_pos.x(), self.text_input_pos.y(), text_width, text_height),
                "color": QColor(self.current_color),
                "width": font_size,
                "text": text
            })
            self.selected_ann = self.annotations[-1]
            self._commit_undo()

        # 隐藏输入框
        self.text_input.hide()
        self.text_input = None
        self.update()

    def _finish_sequence_input(self):
        """完成序号文字输入"""
        if hasattr(self, 'text_input') and self.text_input:
            self.text_input.hide()
            self.text_input = None
        self.current_sequence_ann = None
        self._commit_undo()
        self.update()

    def _add_sequence_annotation(self, pos):
        """添加序号标注"""
        self.sequence_number += 1
        num = self.sequence_number
        circle_radius, _num_pt, _text_pt, text_h, _pad_x = self._seq_metrics()

        ann_data = {
            "tool": "sequence",
            "number": num,
            "circle_pos": QPoint(pos.x(), pos.y()),
            "circle_radius": circle_radius,
            "text_pos": QPoint(pos.x() + circle_radius + 30, pos.y() - text_h),
            "text": "",
            "color": QColor(self.current_color),
            "width": self.current_width,
            "seq_scale": self.current_seq_scale,
        }
        self.annotations.append(ann_data)
        self._commit_undo()
        self.update()

        # 立即显示文字输入框
        self._show_sequence_input_inline(ann_data)

    def _schedule_window_hover_query(self):
        """在选区未确认、未拖动阶段，节流查询光标下的窗口用于高亮。

        用 QTimer 控制 ~80ms 一次，避免 mouseMove 高频枚举全窗口列表。
        """
        if self.selection_confirmed or self.is_selecting or self.is_drawing:
            if self.hover_window_rect is not None:
                self.hover_window_rect = None
                self.update()
            return
        if not self._window_query_scheduled:
            self._window_query_scheduled = True
            self._window_query_timer.start(80)

    def _query_hover_window(self):
        """实际执行窗口查询（在节流计时器触发时）。"""
        self._window_query_scheduled = False
        if self.selection_confirmed or self.is_selecting or self.is_drawing:
            return
        try:
            from core import window_list
            info = window_list.get_window_under_cursor()
        except Exception:
            info = None
        new_rect = info["rect"] if info and info.get("rect") else None
        if new_rect != self.hover_window_rect:
            self.hover_window_rect = new_rect
            self.update()

    def mouseMoveEvent(self, event):
        # 平移中
        if self.panning:
            delta = event.pos() - self.pan_last
            self.pan_last = event.pos()
            self.view_offset = QPointF(self.view_offset.x() + delta.x(),
                                       self.view_offset.y() + delta.y())
            self.update()
            return

        pos = self._img_pos(event.pos())
        # 处理拖动
        if hasattr(self, 'dragging_ann') and self.dragging_ann:
            dx = pos.x() - self.drag_start_pos.x()
            dy = pos.y() - self.drag_start_pos.y()

            ann = self.dragging_ann
            drag_type = self.dragging_type

            if drag_type == "sequence_circle":
                # 只移动圆圈，文字位置不变，连线自动保持连接
                ann["circle_pos"] = QPoint(
                    self.drag_initial_pos.x() + dx,
                    self.drag_initial_pos.y() + dy
                )
            elif drag_type == "sequence_text":
                # 只移动文字区域，圆圈位置不变，连线自动保持连接
                ann["text_pos"] = QPoint(
                    self.drag_initial_pos.x() + dx,
                    self.drag_initial_pos.y() + dy
                )
            elif drag_type == "text_border" and ann.get("tool") == "text":
                # 文字标注的拖动
                rect = ann.get("rect")
                if rect:
                    ann["rect"] = QRect(
                        self.drag_initial_pos.x() + dx,
                        self.drag_initial_pos.y() + dy,
                        rect.width(),
                        rect.height()
                    )
            elif drag_type == "arrow":
                # 拖动整个箭头
                ann["start_pos"] = QPoint(
                    self.drag_initial_start.x() + dx,
                    self.drag_initial_start.y() + dy
                )
                ann["end_pos"] = QPoint(
                    self.drag_initial_end.x() + dx,
                    self.drag_initial_end.y() + dy
                )
            elif drag_type == "point":
                # 拖动端点
                start = ann.get("start_pos")
                end = ann.get("end_pos")
                if self.drag_point_type == "start":
                    ann["start_pos"] = QPoint(
                        self.drag_initial_start.x() + dx,
                        self.drag_initial_start.y() + dy
                    )
                else:
                    ann["end_pos"] = QPoint(
                        self.drag_initial_end.x() + dx,
                        self.drag_initial_end.y() + dy
                    )
            elif drag_type.startswith("handle_"):
                # 控制柄调整大小
                initial_rect = self.drag_initial_rect
                handle_type = self.drag_handle_type

                new_rect = QRect(initial_rect)
                if handle_type == "handle_tl":
                    new_rect.setTopLeft(pos)
                elif handle_type == "handle_t":
                    new_rect.setTop(pos.y())
                elif handle_type == "handle_tr":
                    new_rect.setTopRight(pos)
                elif handle_type == "handle_r":
                    new_rect.setRight(pos.x())
                elif handle_type == "handle_br":
                    new_rect.setBottomRight(pos)
                elif handle_type == "handle_b":
                    new_rect.setBottom(pos.y())
                elif handle_type == "handle_bl":
                    new_rect.setBottomLeft(pos)
                elif handle_type == "handle_l":
                    new_rect.setLeft(pos.x())

                ann["rect"] = new_rect.normalized()
            elif drag_type == "rect_border" or drag_type == "rect":
                # 拖动矩形/椭圆
                rect = ann.get("rect")
                if rect:
                    ann["rect"] = QRect(
                        self.drag_initial_pos.x() + dx,
                        self.drag_initial_pos.y() + dy,
                        rect.width(),
                        rect.height()
                    )
            elif drag_type == "ellipse":
                # 拖动椭圆
                rect = ann.get("rect")
                if rect:
                    ann["rect"] = QRect(
                        self.drag_initial_pos.x() + dx,
                        self.drag_initial_pos.y() + dy,
                        rect.width(),
                        rect.height()
                    )
            elif drag_type in ("pen", "highlight", "mosaic"):
                # 整体平移面状/线状标注
                rect = ann.get("rect")
                if rect:
                    ann["rect"] = QRect(
                        self.drag_initial_pos.x() + dx,
                        self.drag_initial_pos.y() + dy,
                        rect.width(),
                        rect.height()
                    )
                if drag_type == "pen":
                    ann["points"] = [
                        QPoint(self.drag_initial_points[k].x() + dx,
                               self.drag_initial_points[k].y() + dy)
                        for k in range(len(self.drag_initial_points))
                    ]
                elif drag_type == "mosaic":
                    # 马赛克按区域缓存，移动后旧缓存失效，下次绘制重建
                    self._mosaic_cache.clear()
            self.update()
            return

        # 处理选区控制柄拖动
        if hasattr(self, 'selection_dragging') and self.selection_dragging:
            dx = pos.x() - self.selection_drag_start.x()
            dy = pos.y() - self.selection_drag_start.y()
            initial_rect = self.selection_initial_rect
            handle_type = self.selection_drag_type

            new_rect = QRect(initial_rect)
            if handle_type == "handle_tl":
                new_rect.setTopLeft(pos)
            elif handle_type == "handle_t":
                new_rect.setTop(pos.y())
            elif handle_type == "handle_tr":
                new_rect.setTopRight(pos)
            elif handle_type == "handle_r":
                new_rect.setRight(pos.x())
            elif handle_type == "handle_br":
                new_rect.setBottomRight(pos)
            elif handle_type == "handle_b":
                new_rect.setBottom(pos.y())
            elif handle_type == "handle_bl":
                new_rect.setBottomLeft(pos)
            elif handle_type == "handle_l":
                new_rect.setLeft(pos.x())
            elif handle_type == "move":
                new_rect.translate(dx, dy)

            self.selection_rect = new_rect.normalized()
            self._update_toolbar_position()
            self.hover_pos = pos
            self._update_cursor(pos)
            self.update()
            return

        # 根据鼠标位置更新光标状态
        self.hover_pos = pos
        self._update_cursor(pos)

        # 处理选区选择
        if self.is_selecting:
            self.selection_rect = QRect(self.start_point, pos).normalized()
            # 用户开始自定义拖动选区 → 关闭窗口智能高亮
            if self.hover_window_rect is not None:
                self.hover_window_rect = None
            self.update()
        # 处理绘制
        elif self.is_drawing:
            self.current_rect = QRect(self.start_point, pos).normalized()
            self.current_end_pos = pos
            if self.current_tool == "pen" and hasattr(self, 'current_points'):
                self.current_points.append(pos)
            self.update()
        else:
            # 选区未确认、也未在拖动/绘制 → 调度窗口智能识别高亮
            self._schedule_window_hover_query()

    def mouseReleaseEvent(self, event):
        # 结束平移(中键，或拖动工具的左键)
        if self.panning and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self.panning = False
            self._update_cursor(self._img_pos(event.pos()))
            return

        if event.button() == Qt.MouseButton.LeftButton:
            pos = self._img_pos(event.pos())
            # 停止拖动
            if hasattr(self, 'dragging_ann') and self.dragging_ann:
                self.dragging_ann = None
                self.dragging_type = None
                # 拖动/调整大小结束，提交一次撤销快照
                self._commit_undo()

            # 停止选区拖动
            if hasattr(self, 'selection_dragging') and self.selection_dragging:
                self.selection_dragging = False
                self.selection_drag_type = None
                self.update()

            if self.is_selecting:
                self.is_selecting = False
                # 窗口智能选中：若按下时记录了窗口候选，且释放位移很小（视为单击而非
                # 拖动），则直接采用窗口矩形作为选区。
                win_rect = getattr(self, "_window_click_rect", None)
                pressed = getattr(self, "_window_press_pos", None)
                clicked_window = (
                    win_rect is not None and pressed is not None
                    and (pos - pressed).manhattanLength() < 6
                )
                if clicked_window:
                    self.selection_rect = QRect(win_rect)
                    self.hover_window_rect = None
                self._window_click_rect = None
                if self.selection_rect.width() > 10 and self.selection_rect.height() > 10:
                    # 选区有效，确认选区并显示工具栏
                    self.selection_confirmed = True
                    self._show_toolbar()
                else:
                    self.selection_rect = QRect()
                    self.update()
            elif self.is_drawing:
                self.is_drawing = False
                # 判断标注是否有效：箭头/画笔是线状标注，用长度/点数判断；
                # 矩形/椭圆/高亮/马赛克是面状标注，用包围矩形面积判断。
                # 旧逻辑统一要求 width>3 且 height>3，会把水平/垂直箭头、
                # 水平/垂直画笔线误判为无效而丢弃——这正是"最后画的箭头
                # 不在图片里"的根因。
                if self.current_tool == "arrow" or self.current_tool == "line":
                    dx = pos.x() - self.start_point.x()
                    dy = pos.y() - self.start_point.y()
                    is_valid = (dx * dx + dy * dy) > 25  # 长度 > 5px
                elif self.current_tool == "pen" and hasattr(self, 'current_points'):
                    is_valid = len(self.current_points) > 1
                else:
                    is_valid = bool(self.current_rect) and self.current_rect.width() > 3 and self.current_rect.height() > 3

                if is_valid:
                    # 保存标注
                    ann_data = {
                        "tool": self.current_tool,
                        "rect": QRect(self.current_rect),
                        "color": QColor(self.current_color),
                        "fill_color": QColor(self.current_fill_color) if self.current_fill_color else None,
                        "width": self.current_width,
                        "mosaic_size": self.current_mosaic_size,
                        "mosaic_mode": getattr(self, "current_mosaic_mode", "block"),
                        "outline": bool(getattr(self, "current_outline", False)),
                        "outline_color": QColor(getattr(self, "current_outline_color", QColor(255,255,255))),
                    }
                    # 箭头/直线工具保存起点和终点（支持任意方向）
                    if self.current_tool in ("arrow", "line"):
                        ann_data["start_pos"] = QPoint(self.start_point)
                        ann_data["end_pos"] = QPoint(pos)
                        # 直线的实线/虚线
                        if self.current_tool == "line":
                            ann_data["dashed"] = bool(getattr(self, "current_line_dashed", False))
                    # 画笔工具需要保存所有路径点
                    if self.current_tool == "pen" and hasattr(self, 'current_points'):
                        ann_data["points"] = list(self.current_points)
                        self.current_points = []
                    self.annotations.append(ann_data)
                    # 新绘制的标注默认选中(可立即 Delete 删除)，并提交撤销快照
                    self.selected_ann = ann_data
                    self._commit_undo()
                self.current_rect = None
                self.update()

    def _available_area(self):
        """工具栏定位用的可用区域（逻辑点）。

        macOS 上需排除底部 Dock（用 QScreen.availableGeometry，而非选区窗口的
        rect——后者铺满整个虚拟桌面，含 Dock 区域，会把工具栏摆到 Dock 后面）。
        返回 (x, y, width, height)。
        """
        if is_macos():
            # 合并所有屏的可用区域并集（已自动排除菜单栏与 Dock）
            from PyQt6.QtGui import QGuiApplication
            geo = None
            for s in QGuiApplication.screens():
                g = s.availableGeometry()
                geo = QRect(g) if geo is None else geo.united(g)
            if geo is not None:
                return geo.x(), geo.y(), geo.width(), geo.height()
        return 0, 0, self.rect().width(), self.rect().height()

    def _toolbar_pos(self):
        """计算工具栏位置：优先选区右下角，超出屏幕则改放选区右上角；
        若选区占满整屏(滚动截图结果)两处都会越界，则兜底贴底显示，确保始终可见。
        垂直方向按"主工具栏 + 样式子面板"整体高度做越界判断,保证两者都可见。"""
        toolbar_width = self.toolbar.width()
        toolbar_height = self.toolbar.height()
        ax, ay, win_w, win_h = self._available_area()

        x = self.selection_rect.right() - toolbar_width - 10
        y = self.selection_rect.bottom() + 10
        total_h = toolbar_height + self.style_panel_height()
        if y + total_h > ay + win_h:
            y = self.selection_rect.top() - total_h - 10
        # 兜底：保证工具栏(+子面板)始终落在可视区域内
        if y < ay:
            y = max(ay + 10, ay + win_h - total_h - 10)
        if x < ax:
            x = ax
        elif x + toolbar_width > ax + win_w:
            x = max(ax, ax + win_w - toolbar_width - 10)
        return x, y

    def style_panel_height(self):
        """子面板占用的垂直高度(含与主工具栏 4px 间距);pan 或子面板未建返回 0。"""
        if not hasattr(self, 'style_panel') or self.current_tool == "pan":
            return 0
        return self.style_panel.height() + 4

    def _style_panel_pos(self):
        """子面板位置:主工具栏正下方、左边缘对齐;右/下越界则贴边。"""
        x = self.toolbar.x()
        y = self.toolbar.y() + self.toolbar.height() + 4
        sp_w = self.style_panel.width()
        sp_h = self.style_panel.height()
        ax, ay, win_w, win_h = self._available_area()
        if x + sp_w > ax + win_w:
            x = max(ax, ax + win_w - sp_w - 10)
        if y + sp_h > ay + win_h:
            y = ay + win_h - sp_h - 10
        if y < ay:
            y = ay
        return x, y

    def _show_toolbar(self):
        """显示底部工具栏 + 样式子面板"""
        if not hasattr(self, 'toolbar'):
            self._create_toolbar()
        if not hasattr(self, 'style_panel'):
            self._create_style_panel()

        # 工具栏/子面板作为独立顶层浮层窗口（不要 setParent：子部件无法使用
        # WindowStaysOnTopHint/Tool 层级标志，也无法浮于 Dock 之上）。
        # macOS 上 overlay_window_flags() 额外加 Qt.Tool，保证盖住 Dock。
        self.toolbar.setWindowFlags(overlay_window_flags())
        self.style_panel.setWindowFlags(overlay_window_flags())

        self.toolbar.move(*self._toolbar_pos())
        self.toolbar.show()
        # macOS：把工具栏 NSWindow 提到 Dock 之上
        raise_overlay(self.toolbar)
        # 子面板随当前工具显隐 + 定位(pan 不显示)
        self._update_style_panel()

        # 改变光标为正常箭头，方便标注操作
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        # macOS：选区确认后焦点可能已跑到被截应用（窗口截图单击后尤其明显），
        # 导致 Esc（QShortcut）不响应。这里把激活焦点拉回选区窗口。
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _update_toolbar_position(self):
        """更新工具栏与子面板位置（选区改变时调用）"""
        if not hasattr(self, 'toolbar'):
            return
        self.toolbar.move(*self._toolbar_pos())
        if hasattr(self, 'style_panel') and self.style_panel.isVisible():
            self.style_panel.move(*self._style_panel_pos())

    def _create_toolbar(self):
        """创建工具栏（参考ishort工具栏布局）"""
        toolbar_height = 36

        self.toolbar = QFrame()
        # 仅固定高度；宽度在按钮全部添加后按内容自适应，避免写死宽度裁掉尾部按钮
        self.toolbar.setFixedHeight(toolbar_height)
        self.toolbar.setWindowFlags(overlay_window_flags())
        self.toolbar.setStyleSheet("""
            QFrame {
                background-color: rgba(35, 35, 35, 240);
                border-radius: 8px;
            }
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 32);
            }
            QPushButton:pressed {
                background-color: rgba(255, 255, 255, 55);
            }
            QPushButton.active {
                background-color: #0a84ff;
            }
            QToolTip {
                background-color: rgba(35, 35, 35, 245);
                color: #e5e7eb;
                border: 1px solid rgba(255, 255, 255, 45);
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
            }
        """)

        # 工具栏布局
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(12, 4, 12, 4)
        self.toolbar_layout.setSpacing(4)

        # 工具按钮配置 - 使用绘制函数
        tool_configs = [
            ("arrow", self._create_arrow_icon, "箭头"),
            ("line", self._create_line_icon, "直线"),
            ("rectangle", self._create_rect_icon, "矩形"),
            ("ellipse", self._create_ellipse_icon, "椭圆"),
            ("text", self._create_text_icon, "文字"),
            ("pen", self._create_pen_icon, "画笔"),
            ("highlight", self._create_highlight_icon, "高亮"),
            ("mosaic", self._create_mosaic_icon, "马赛克"),
            ("sequence", self._create_sequence_icon, "序号"),
            ("picker", self._create_picker_icon, "取色器 (吸管)"),
            ("pan", self._create_pan_icon, "拖动 (平移图像)"),
        ]

        self.tool_buttons = {}
        for tool_id, icon_func, tip in tool_configs:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # 绘制图标
            icon = icon_func()
            btn.setIcon(icon)
            btn.setIconSize(QSize(20, 20))
            btn.clicked.connect(lambda checked, t=tool_id: self._select_tool(t))
            self.tool_buttons[tool_id] = btn
            self.toolbar_layout.addWidget(btn)

        # 分割线(工具区与操作区分隔;色板/填充/线宽/字号等样式控件已迁至样式子面板)
        self.toolbar_layout.addWidget(self._create_separator(toolbar_height))

        # 撤销 / 重做
        undo_btn = QPushButton()
        undo_btn.setFixedSize(28, 28)
        undo_btn.setIcon(self._create_undo_icon())
        undo_btn.setIconSize(QSize(20, 20))
        undo_btn.setToolTip("撤销 (Ctrl+Z)")
        undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        undo_btn.clicked.connect(self._undo)
        self.toolbar_layout.addWidget(undo_btn)

        redo_btn = QPushButton()
        redo_btn.setFixedSize(28, 28)
        redo_btn.setIcon(self._create_redo_icon())
        redo_btn.setIconSize(QSize(20, 20))
        redo_btn.setToolTip("重做 (Ctrl+Y / Ctrl+Shift+Z)")
        redo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        redo_btn.clicked.connect(self._redo)
        self.toolbar_layout.addWidget(redo_btn)

        # 分割线
        self.toolbar_layout.addWidget(self._create_separator(toolbar_height))

        # 滚动截图按钮
        scroll_btn = QPushButton()
        scroll_btn.setFixedSize(28, 28)
        scroll_btn.setIcon(self._create_scroll_icon())
        scroll_btn.setIconSize(QSize(20, 20))
        scroll_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        scroll_btn.setToolTip("滚动截图")
        scroll_btn.clicked.connect(self._start_scroll_capture)
        self.toolbar_layout.addWidget(scroll_btn)

        # GIF 录制按钮（图标风格 + REC 文字，与其他工具图标统一）
        gif_btn = QPushButton()
        gif_btn.setFixedSize(28, 28)
        gif_btn.setIcon(self._create_gif_icon())
        gif_btn.setIconSize(QSize(20, 20))
        gif_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gif_btn.setToolTip("录制 GIF")
        gif_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:4px;"
            "font-size:7px;color:#ddd;font-weight:bold;}"
            "QPushButton:hover{background:rgba(255,255,255,32);}")
        gif_btn.clicked.connect(self._start_gif_record)
        self.toolbar_layout.addWidget(gif_btn)

        # OCR 按钮（仅 macOS 显示）
        if is_macos():
            ocr_btn = QPushButton()
            ocr_btn.setFixedSize(28, 28)
            ocr_btn.setIcon(self._create_ocr_icon())
            ocr_btn.setIconSize(QSize(20, 20))
            ocr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ocr_btn.setToolTip("识别文字 (OCR)")
            ocr_btn.clicked.connect(self._run_ocr)
            self.toolbar_layout.addWidget(ocr_btn)

        # 分割线
        self.toolbar_layout.addWidget(self._create_separator(toolbar_height))

        # 保存按钮 - 绘制下载图标
        save_btn = QPushButton()
        save_btn.setFixedSize(28, 28)
        save_btn.setIcon(self._create_save_icon())
        save_btn.setIconSize(QSize(20, 20))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setToolTip("保存到文件")
        save_btn.clicked.connect(self._save_to_file)
        self.toolbar_layout.addWidget(save_btn)

        # 关闭/取消按钮 - 放在勾号前面
        close_btn = QPushButton()
        close_btn.setFixedSize(28, 28)
        close_btn.setIcon(self._create_close_icon())
        close_btn.setIconSize(QSize(20, 20))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("取消截图")
        close_btn.clicked.connect(self._cancel_screenshot)
        self.toolbar_layout.addWidget(close_btn)

        # 完成按钮 - 与其他图标风格一致
        done_btn = QPushButton()
        done_btn.setFixedSize(28, 28)
        done_btn.setIcon(self._create_check_icon())
        done_btn.setIconSize(QSize(20, 20))
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.setToolTip("完成 (复制到剪贴板)")
        done_btn.clicked.connect(self._copy_to_clipboard)
        self.toolbar_layout.addWidget(done_btn)

        # 按实际内容收缩宽度，避免固定宽度裁掉滚动/保存/关闭/完成等尾部按钮
        self.toolbar.setFixedWidth(self.toolbar_layout.sizeHint().width())

        # 工具栏初始隐藏
        self.toolbar.hide()

        # 默认选中工具(仅高亮工具按钮;色板/线宽/字号的高亮在 _create_style_panel 末尾完成)
        self._select_tool(self.current_tool)

    def _create_style_panel(self):
        """样式子面板:选中工具时在主工具栏正下方弹出,横向展示该工具的样式控件。
        暗色药丸风格(与主工具栏一致)。各样式组按工具显隐(见 _update_style_panel)。"""
        self.style_panel = QFrame()
        self.style_panel.setFixedHeight(36)
        self.style_panel.setWindowFlags(overlay_window_flags())
        self.style_panel.setStyleSheet("""
            QFrame {
                background-color: rgba(35, 35, 35, 240);
                border-radius: 8px;
            }
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 32);
            }
            QPushButton:pressed {
                background-color: rgba(255, 255, 255, 55);
            }
            QPushButton.active {
                background-color: #0a84ff;
            }
        """)
        self.style_panel_layout = QHBoxLayout(self.style_panel)
        self.style_panel_layout.setContentsMargins(12, 4, 12, 4)
        self.style_panel_layout.setSpacing(4)

        colors = [
            ("#ff3b30", "红色"),
            ("#007aff", "蓝色"),
            ("#ffcc00", "黄色"),
            ("#34c759", "绿色"),
            ("#000000", "黑色"),
            ("#ffffff", "白色"),
        ]

        # 边框/线条/文字色板(首组,无前导分隔线);除马赛克外的绘图工具使用
        self.color_group = QWidget()
        _cg = QHBoxLayout(self.color_group)
        _cg.setContentsMargins(0, 0, 0, 0)
        _cg.setSpacing(4)
        self.color_buttons = {}
        for color_hex, name in colors:
            btn = QPushButton()
            btn.setFixedSize(14, 14)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color_hex};
                    border-radius: 7px;
                    border: 1px solid rgba(255,255,255,48);
                }}
                QPushButton:hover {{
                    border: 1.5px solid white;
                }}
            """)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            color = QColor(color_hex)
            btn.clicked.connect(lambda checked, c=color, h=color_hex: self._select_color(c, h))
            self.color_buttons[color_hex] = btn
            _cg.addWidget(btn)
        self.style_panel_layout.addWidget(self.color_group)

        # 填充色组(矩形/椭圆):分隔线 + 无填充 + 6 色
        self.fill_group = QWidget()
        _fg = QHBoxLayout(self.fill_group)
        _fg.setContentsMargins(0, 0, 0, 0)
        _fg.setSpacing(4)
        _fg.addWidget(self._create_separator(36))
        _nofill = QPushButton()
        _nofill.setFixedSize(14, 14)
        _nofill.setIcon(QIcon(self._create_nofill_pixmap()))
        _nofill.setIconSize(QSize(12, 12))
        _nofill.setToolTip("无填充")
        _nofill.setCursor(Qt.CursorShape.PointingHandCursor)
        _nofill.clicked.connect(lambda checked: self._select_fill_color(None))
        self.fill_buttons = {None: _nofill}
        _fg.addWidget(_nofill)
        for _hex, _name in colors:
            _b = QPushButton()
            _b.setFixedSize(14, 14)
            _b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {_hex};
                    border-radius: 7px;
                    border: 1px solid rgba(255,255,255,48);
                }}
                QPushButton:hover {{
                    border: 1.5px solid white;
                }}
            """)
            _b.setCursor(Qt.CursorShape.PointingHandCursor)
            _b.clicked.connect(lambda checked, c=QColor(_hex), h=_hex: self._select_fill_color(c, h))
            self.fill_buttons[_hex] = _b
            _fg.addWidget(_b)
        self.fill_group.setVisible(False)
        self.style_panel_layout.addWidget(self.fill_group)

        # 线宽组(矩形/椭圆/箭头/画笔/高亮):分隔线 + 1/2/4 由细到粗
        self.width_group = QWidget()
        _wg = QHBoxLayout(self.width_group)
        _wg.setContentsMargins(0, 0, 0, 0)
        _wg.setSpacing(4)
        _wg.addWidget(self._create_separator(36))
        widths = [1, 2, 4]
        self.width_buttons = {}
        for w in widths:
            btn = QPushButton()
            btn.setFixedSize(20, 20)
            btn.setIcon(self._create_width_icon(w))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(f"线宽 {w}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, width=w: self._select_width(width))
            self.width_buttons[w] = btn
            _wg.addWidget(btn)
        self.width_group.setVisible(False)
        self.style_panel_layout.addWidget(self.width_group)

        # 直线样式组(实线/虚线):仅 line 工具显示
        self.current_line_dashed = False
        self.line_style_group = QWidget()
        _lsg = QHBoxLayout(self.line_style_group)
        _lsg.setContentsMargins(0, 0, 0, 0)
        _lsg.setSpacing(4)
        _lsg.addWidget(self._create_separator(36))
        self.line_style_buttons = {}
        for dashed, tip in [(False, "实线"), (True, "虚线")]:
            btn = QPushButton()
            btn.setFixedSize(20, 20)
            btn.setIcon(self._create_line_style_icon(dashed))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, d=dashed: self._select_line_style(d))
            self.line_style_buttons[dashed] = btn
            _lsg.addWidget(btn)
        self.line_style_group.setVisible(False)
        self.style_panel_layout.addWidget(self.line_style_group)

        # 字号组(文字):分隔线 + 12/16/22 "A" 由小到大,与线宽解耦
        self.text_size_group = QWidget()
        _tg = QHBoxLayout(self.text_size_group)
        _tg.setContentsMargins(0, 0, 0, 0)
        _tg.setSpacing(4)
        _tg.addWidget(self._create_separator(36))
        self._text_sizes = [12, 16, 22]
        self.text_size_buttons = {}
        for _i, _ts in enumerate(self._text_sizes):
            btn = QPushButton("A")
            btn.setFixedSize(20, 20)
            btn.setFont(QFont("Microsoft YaHei", 8 + _i * 2))  # 按钮上 A 字号递增示意
            btn.setToolTip(f"字号 {_ts}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, s=_ts: self._select_text_size(s))
            self.text_size_buttons[_ts] = btn
            _tg.addWidget(btn)
        self.text_size_group.setVisible(False)
        self.style_panel_layout.addWidget(self.text_size_group)

        # 马赛克网格大小组(马赛克):分隔线 + 粗14/中10/细6(块像素越大越粗)
        self.mosaic_size_group = QWidget()
        _mg = QHBoxLayout(self.mosaic_size_group)
        _mg.setContentsMargins(0, 0, 0, 0)
        _mg.setSpacing(4)
        _mg.addWidget(self._create_separator(36))
        self._mosaic_sizes = [14, 10, 6]  # 顺序:粗→中→细
        self._mosaic_labels = {14: "粗", 10: "中", 6: "细"}
        self.mosaic_size_buttons = {}
        for ms in self._mosaic_sizes:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setIcon(self._create_mosaic_size_icon(ms))
            btn.setIconSize(QSize(20, 20))
            btn.setToolTip(f"马赛克 {self._mosaic_labels[ms]}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, s=ms: self._select_mosaic_size(s))
            self.mosaic_size_buttons[ms] = btn
            _mg.addWidget(btn)
        self.mosaic_size_group.setVisible(False)
        self.style_panel_layout.addWidget(self.mosaic_size_group)

        # 马赛克模式组:块状/模糊
        self.current_mosaic_mode = "block"
        self.mosaic_mode_group = QWidget()
        _mmg = QHBoxLayout(self.mosaic_mode_group)
        _mmg.setContentsMargins(0, 0, 0, 0)
        _mmg.setSpacing(4)
        _mmg.addWidget(self._create_separator(36))
        self.mosaic_mode_buttons = {}
        for mode, tip in [("block", "块状马赛克"), ("blur", "高斯模糊")]:
            btn = QPushButton()
            btn.setFixedSize(20, 20)
            btn.setIcon(self._create_mosaic_mode_icon(mode))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, m=mode: self._select_mosaic_mode(m))
            self.mosaic_mode_buttons[mode] = btn
            _mmg.addWidget(btn)
        self.mosaic_mode_group.setVisible(False)
        self.style_panel_layout.addWidget(self.mosaic_mode_group)

        # 描边组(所有绘图工具):开关 + 描边色
        self.current_outline = False
        self.current_outline_color = QColor(255, 255, 255)
        self.outline_group = QWidget()
        _og = QHBoxLayout(self.outline_group)
        _og.setContentsMargins(0, 0, 0, 0)
        _og.setSpacing(4)
        _og.addWidget(self._create_separator(36))
        # 描边开关
        self.outline_toggle = QPushButton("描边")
        self.outline_toggle.setFixedHeight(20)
        self.outline_toggle.setCheckable(True)
        self.outline_toggle.setToolTip("给标注加描边(复杂背景上更清晰)")
        self.outline_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outline_toggle.setStyleSheet(
            "QPushButton{background:transparent;border:1px solid #555;border-radius:4px;"
            "padding:0 8px;color:#ddd;font-size:11px;}"
            "QPushButton[active=\"true\"]{background:#0a84ff;border-color:#0a84ff;}")
        self.outline_toggle.clicked.connect(self._toggle_outline)
        _og.addWidget(self.outline_toggle)
        # 描边色(黑/白两选,最常用)
        self.outline_color_buttons = {}
        for oc, tip in [(QColor(255,255,255), "白描边"), (QColor(0,0,0), "黑描边")]:
            btn = QPushButton()
            btn.setFixedSize(20, 20)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{background:{oc.name()};border:1px solid #555;border-radius:4px;}}"
                f"QPushButton[active=\"true\"]{{border:2px solid #0a84ff;}}")
            btn.clicked.connect(lambda checked, c=oc: self._select_outline_color(c))
            self.outline_color_buttons[oc.name()] = btn
            _og.addWidget(btn)
        self.outline_group.setVisible(False)
        self.style_panel_layout.addWidget(self.outline_group)

        # 序号大小组(序号):分隔线 + 小0.85/中1.0/大1.25
        self.seq_scale_group = QWidget()
        _sg = QHBoxLayout(self.seq_scale_group)
        _sg.setContentsMargins(0, 0, 0, 0)
        _sg.setSpacing(4)
        _sg.addWidget(self._create_separator(36))
        self._seq_scales = [0.85, 1.0, 1.25]
        self._seq_labels = {0.85: "小", 1.0: "中", 1.25: "大"}
        self.seq_scale_buttons = {}
        for sc in self._seq_scales:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setIcon(self._create_seq_scale_icon(sc))
            btn.setIconSize(QSize(20, 20))
            btn.setToolTip(f"序号 {self._seq_labels[sc]}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, s=sc: self._select_seq_scale(s))
            self.seq_scale_buttons[sc] = btn
            _sg.addWidget(btn)
        self.seq_scale_group.setVisible(False)
        self.style_panel_layout.addWidget(self.seq_scale_group)

        # 按内容收缩宽度,初始隐藏(随工具显隐)
        self.style_panel.setFixedWidth(self.style_panel_layout.sizeHint().width())
        self.style_panel.hide()

        # 初始化各样式高亮(使用 __init__ 中可能来自 QSettings 的当前值)
        self._select_color(self.current_color, self.current_color.name())
        self._select_width(self.current_width)
        self._select_text_size(self.current_text_size)
        self._update_fill_buttons()
        self._select_mosaic_size(self.current_mosaic_size)
        self._select_seq_scale(self.current_seq_scale)
        # 按 current_tool 切换各组显隐 + 子面板宽度/定位
        self._update_style_panel()

    # ---- 统一图标设计系统(中性浅灰为主;仅 close/check 保留语义色)----
    ICON_GRAY = QColor(229, 231, 235)      # 主色:工具/历史/操作图标
    ICON_GRAY_DIM = QColor(160, 166, 176)  # 次级:马赛克深格 / 序号字
    ICON_RED = QColor(255, 95, 86)         # close(柔和红)
    ICON_GREEN = QColor(75, 205, 110)      # check(柔和绿)

    def _begin_icon(self, color=None, width=2.0):
        """统一图标底：24×24 透明画布 + 抗锯齿 + 圆头圆角描边。
        返回 (pixmap, painter)；调用方画完形状后自行 painter.end()。"""
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color or self.ICON_GRAY, width,
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        return pixmap, painter

    def _create_arrow_icon(self):
        """箭头(中性)"""
        pm, p = self._begin_icon()
        tip = QPointF(18.5, 5.5)
        p.drawLine(QPointF(4.5, 18.5), tip)   # 杆
        p.drawLine(tip, QPointF(11.5, 6.0))   # 箭头头
        p.drawLine(tip, QPointF(18.0, 11.5))
        p.end()
        return QIcon(pm)

    def _create_line_icon(self):
        """直线(中性,无箭头的斜线)"""
        pm, p = self._begin_icon()
        p.drawLine(QPointF(4.5, 18.5), QPointF(18.5, 5.5))
        p.end()
        return QIcon(pm)

    def _create_picker_icon(self):
        """取色器/吸管(中性)"""
        pm, p = self._begin_icon()
        # 吸管杆
        p.drawLine(QPointF(16, 4), QPointF(7, 13))
        # 吸管尖端(深色三角)
        p.setBrush(QBrush(self.ICON_GRAY))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPointF(7, 13), QPointF(9, 11), QPointF(5, 15))
        p.end()
        return QIcon(pm)

    def _create_undo_icon(self):
        """撤销(中性,圆弧+箭头)"""
        pm, p = self._begin_icon()
        p.drawArc(QRectF(4, 4, 16, 16), 0, 270 * 16)
        p.drawLine(QPointF(12, 4), QPointF(8, 4))
        p.drawLine(QPointF(12, 4), QPointF(12, 8))
        p.end()
        return QIcon(pm)

    def _create_redo_icon(self):
        """重做(中性,圆弧+箭头)"""
        pm, p = self._begin_icon()
        p.drawArc(QRectF(4, 4, 16, 16), 0, -270 * 16)
        p.drawLine(QPointF(12, 4), QPointF(16, 4))
        p.drawLine(QPointF(12, 4), QPointF(12, 8))
        p.end()
        return QIcon(pm)

    def _create_rect_icon(self):
        """矩形(中性,圆角)"""
        pm, p = self._begin_icon()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2.5, 2.5)
        p.end()
        return QIcon(pm)

    def _create_ellipse_icon(self):
        """椭圆(中性)"""
        pm, p = self._begin_icon()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(3.5, 4.5, 17, 15))
        p.end()
        return QIcon(pm)

    def _create_text_icon(self):
        """文字 T(中性)"""
        pm, p = self._begin_icon()
        p.setFont(QFont("", 12, QFont.Weight.Bold))
        p.drawText(QRectF(3, 3, 18, 18), Qt.AlignmentFlag.AlignCenter, "T")
        p.end()
        return QIcon(pm)

    def _create_pen_icon(self):
        """画笔(中性,斜笔+笔帽)"""
        pm, p = self._begin_icon()
        p.drawLine(QPointF(6, 18), QPointF(16, 8))    # 笔身
        p.drawLine(QPointF(14, 6), QPointF(18, 10))   # 笔帽
        p.end()
        return QIcon(pm)

    def _create_highlight_icon(self):
        """高亮(马克笔,中性;粗笔身+细笔尖)"""
        pm, p = self._begin_icon(width=3.2)
        p.drawLine(QPointF(9.0, 15.0), QPointF(15.0, 9.0))   # 笔身(粗)
        p.setPen(QPen(self.ICON_GRAY, 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(6.0, 18.0), QPointF(9.0, 15.0))   # 笔尖(细)
        p.end()
        return QIcon(pm)

    def _create_mosaic_icon(self):
        """马赛克(4×4 网格,两种灰度交错,中性)"""
        pm, p = self._begin_icon()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(4):
            for j in range(4):
                x, y = 3.5 + j * 4.25, 3.5 + i * 4.25
                p.setBrush(QBrush(self.ICON_GRAY if (i + j) % 2 == 0 else self.ICON_GRAY_DIM))
                p.drawRoundedRect(QRectF(x, y, 3.3, 3.3), 0.7, 0.7)
        p.end()
        return QIcon(pm)

    def _create_sequence_icon(self):
        """序号(描边圆 + 数字,中性)"""
        pm, p = self._begin_icon()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
        p.setFont(QFont("", 10, QFont.Weight.Bold))
        p.drawText(QRectF(3.5, 3.5, 17, 17), Qt.AlignmentFlag.AlignCenter, "1")
        p.end()
        return QIcon(pm)

    def _create_save_icon(self):
        """保存/下载(中性,下箭头+底线)"""
        pm, p = self._begin_icon()
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawLine(QPointF(7, 10), QPointF(12, 15))
        p.drawLine(QPointF(17, 10), QPointF(12, 15))
        p.drawLine(QPointF(5, 19), QPointF(19, 19))
        p.end()
        return QIcon(pm)

    def _create_scroll_icon(self):
        """滚动截图(中性,竖条+下箭头)"""
        pm, p = self._begin_icon()
        fill = QColor(self.ICON_GRAY)
        fill.setAlpha(55)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(QRectF(8, 3, 8, 18), 2.5, 2.5)
        p.drawLine(QPointF(12, 7), QPointF(12, 15))
        p.drawLine(QPointF(9.2, 12.2), QPointF(12, 15))
        p.drawLine(QPointF(14.8, 12.2), QPointF(12, 15))
        p.end()
        return QIcon(pm)

    def _create_gif_icon(self):
        """GIF 录制(中性,胶片框 + REC 文字)"""
        pm, p = self._begin_icon()
        p.setBrush(QBrush(QColor(240, 240, 240)))
        p.setPen(Qt.PenStyle.NoPen)
        # 白色胶片框
        p.drawRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        # REC 文字（深色，白色底上可见）
        p.setPen(QPen(QColor(40, 40, 40)))
        f = QFont("Arial", 6, QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(3, 5, 18, 14), Qt.AlignmentFlag.AlignCenter, "REC")
        p.end()
        return QIcon(pm)

    def _create_ocr_icon(self):
        """OCR 识别文字(中性,A 加识别框)"""
        pm, p = self._begin_icon()
        # 虚线识别框
        pen = QPen(self.ICON_GRAY, 1.2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(3.5, 5.5, 17, 12))
        # 中间 A
        p.setPen(QPen(self.ICON_GRAY, 1.5))
        p.drawLine(QPointF(9, 14), QPointF(12, 7))
        p.drawLine(QPointF(12, 7), QPointF(15, 14))
        p.drawLine(QPointF(10, 11.5), QPointF(14, 11.5))
        p.end()
        return QIcon(pm)

    def _create_check_icon(self):
        """完成(柔和绿勾)"""
        pm, p = self._begin_icon(self.ICON_GREEN, 2.6)
        p.drawLine(QPointF(5, 12), QPointF(10, 17))
        p.drawLine(QPointF(10, 17), QPointF(19, 7.5))
        p.end()
        return QIcon(pm)

    def _create_close_icon(self):
        """关闭/取消(柔和红 X)"""
        pm, p = self._begin_icon(self.ICON_RED, 2.4)
        p.drawLine(QPointF(7, 7), QPointF(17, 17))
        p.drawLine(QPointF(17, 7), QPointF(7, 17))
        p.end()
        return QIcon(pm)

    def _create_pan_icon(self):
        """拖动/平移图标：张开的手(抓手,中性)。"""
        pm, p = self._begin_icon()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self.ICON_GRAY))
        finger_w = 2.6
        # 四根手指(顶部圆角竖条)
        for fx in (6.0, 9.4, 12.8, 16.2):
            p.drawRoundedRect(QRectF(fx, 4.0, finger_w, 9.0), 1.3, 1.3)
        # 拇指(左下斜向圆角条)
        p.save()
        p.translate(6.8, 15.8)
        p.rotate(-35)
        p.drawRoundedRect(QRectF(-1.3, -4.5, 2.6, 8.5), 1.3, 1.3)
        p.restore()
        # 手掌(底部圆角块，连接手指)
        p.drawRoundedRect(QRectF(5.2, 10.5, 13.6, 9.0), 3.2, 3.2)
        p.end()
        return QIcon(pm)

    def _create_width_icon(self, w):
        """线宽示意：由细到粗的水平横线(中性,w=2/4/6)。"""
        pm, p = self._begin_icon(width=float(w))
        p.drawLine(QPointF(4, 12), QPointF(20, 12))
        p.end()
        return QIcon(pm)

    def _create_mosaic_size_icon(self, size):
        """马赛克网格大小示意:粗(2×2)/中(3×3)/细(4×4),中性灰描边方框。"""
        pm, p = self._begin_icon()
        n = {14: 2, 10: 3, 6: 4}[size]
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(3.5, 3.5, 17, 17))
        step = 17.0 / n
        for i in range(1, n):
            v = 3.5 + i * step
            p.drawLine(QPointF(v, 3.5), QPointF(v, 20.5))
            p.drawLine(QPointF(3.5, v), QPointF(20.5, v))
        p.end()
        return QIcon(pm)

    def _create_seq_scale_icon(self, scale):
        """序号大小示意:实心圆点半径随缩放,中性灰。"""
        pm, p = self._begin_icon()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self.ICON_GRAY))
        r = 3.5 + scale * 4.0  # 小~6.9 / 中~7.5 / 大~8.5
        p.drawEllipse(QPointF(12, 12), r, r)
        p.end()
        return QIcon(pm)

    def _create_separator(self, height):
        """创建分割线"""
        sep = QFrame()
        sep.setFixedSize(1, 20)
        sep.setStyleSheet("background-color: rgba(255,255,255,65);")
        return sep

    def _select_color(self, color, color_hex=None):
        self.current_color = color
        if color_hex:
            self._settings.setValue("color", color_hex)
        # 更新颜色按钮选中状态
        for hex_btn, btn in self.color_buttons.items():
            if color_hex and hex_btn == color_hex:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {hex_btn};
                        border-radius: 10px;
                        border: 2px solid #007aff;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {hex_btn};
                        border-radius: 10px;
                        border: 1px solid rgba(255,255,255,48);
                    }}
                """)

    def _select_fill_color(self, color, color_hex=None):
        """设置填充色(None=无填充),仅矩形/椭圆有效。"""
        self.current_fill_color = color
        self._update_fill_buttons(color_hex)

    def _update_fill_buttons(self, color_hex=None):
        """更新填充色按钮高亮:选中加蓝框。"""
        for key, btn in self.fill_buttons.items():
            bg = "transparent" if key is None else key
            if (key is None and color_hex is None) or (color_hex and key == color_hex):
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {bg};
                        border-radius: 7px;
                        border: 2px solid #007aff;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {bg};
                        border-radius: 7px;
                        border: 1px solid rgba(255,255,255,48);
                    }}
                """)

    def _apply_fill(self, painter, ann):
        """按标注 fill_color 设置画刷(半透明填充);无填充则 NoBrush。"""
        fill = ann.get("fill_color")
        if fill is not None:
            f = QColor(fill)
            f.setAlpha(self._FILL_ALPHA)
            painter.setBrush(QBrush(f))
        else:
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    def _create_nofill_pixmap(self):
        """无填充色块图标:透明圆 + 灰斜杠(14x14)。"""
        pm = QPixmap(14, 14)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self.ICON_GRAY_DIM, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(1.0, 1.0, 12.0, 12.0))
        p.setPen(QPen(self.ICON_GRAY_DIM, 1.4))
        p.drawLine(QPointF(3.8, 10.2), QPointF(10.2, 3.8))
        p.end()
        return pm

    def _select_tool(self, tool_id):
        self.current_tool = tool_id
        # "pan" 是临时导航模式，不写入 QSettings——避免覆盖上次绘图工具，
        # 也避免重启后停留在平移模式。
        if tool_id != "pan":
            self._settings.setValue("tool", tool_id)
        # 更新工具按钮高亮
        for btn in self.tool_buttons.values():
            btn.setProperty("active", False)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        btn = self.tool_buttons[tool_id]
        btn.setProperty("active", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        # 样式子面板各组按工具显隐 + 子面板宽度/定位(style_panel 未建时跳过)
        self._update_style_panel()

    def _update_style_panel(self):
        """按 current_tool 切换样式子面板各组的显隐,并重算子面板宽度与定位。"""
        if not hasattr(self, 'style_panel'):
            return
        t = self.current_tool
        # 色板:除马赛克外的所有绘图工具(马赛克无颜色概念);picker 是工具无样式
        self.color_group.setVisible(
            t in ("rectangle", "ellipse", "arrow", "line", "pen", "highlight", "text", "sequence"))
        self.fill_group.setVisible(t in ("rectangle", "ellipse"))
        self.width_group.setVisible(
            t in ("rectangle", "ellipse", "arrow", "line", "pen", "highlight"))
        self.line_style_group.setVisible(t == "line")
        self.text_size_group.setVisible(t == "text")
        self.mosaic_size_group.setVisible(t == "mosaic")
        self.mosaic_mode_group.setVisible(t == "mosaic")
        self.outline_group.setVisible(
            t in ("rectangle", "ellipse", "arrow", "line", "pen", "highlight", "text"))
        self.seq_scale_group.setVisible(t == "sequence")
        # pan/picker 无样式 → 隐藏整个子面板;其余显示
        show_panel = t not in ("pan", "picker")
        self.style_panel.setVisible(show_panel)
        if show_panel:
            self.style_panel.setFixedWidth(self.style_panel_layout.sizeHint().width())
            # 子面板显隐改变整体高度,需重算主工具栏位置,再定位子面板
            if hasattr(self, 'toolbar') and self.toolbar.isVisible():
                self.toolbar.move(*self._toolbar_pos())
            self.style_panel.move(*self._style_panel_pos())
            self.style_panel.show()  # setWindowFlags 后需重新 show 才生效
            raise_overlay(self.style_panel)  # macOS 提到 Dock 之上

    def _select_width(self, width):
        self.current_width = width
        self._settings.setValue("width", width)
        # 更新宽度按钮选中状态
        for w, btn in self.width_buttons.items():
            if w == width:
                btn.setProperty("active", True)
            else:
                btn.setProperty("active", False)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _select_line_style(self, dashed):
        """设置直线样式:实线/虚线。"""
        self.current_line_dashed = dashed
        for d, btn in self.line_style_buttons.items():
            btn.setProperty("active", d == dashed)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _create_line_style_icon(self, dashed):
        """直线样式图标:实线/虚线。"""
        pm, p = self._begin_icon()
        pen = QPen(self.ICON_GRAY, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(3, 16), QPointF(17, 16))
        p.end()
        return QIcon(pm)

    def _select_text_size(self, size):
        """设置文字字号(与线宽解耦),仅 text 工具用。"""
        self.current_text_size = size
        self._settings.setValue("text_size", size)
        for s, btn in self.text_size_buttons.items():
            btn.setProperty("active", s == size)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _select_mosaic_size(self, size):
        """设置马赛克网格大小(粗14/中10/细6),仅 mosaic 工具用。"""
        self.current_mosaic_size = size
        self._settings.setValue("mosaic_size", size)
        for s, btn in self.mosaic_size_buttons.items():
            btn.setProperty("active", s == size)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        # 切粗细后马赛克缓存失效（blur 模式也按 size 定 radius）
        self._mosaic_cache.clear()
        self.update()

    def _select_mosaic_mode(self, mode):
        """设置马赛克模式:块状/模糊。"""
        self.current_mosaic_mode = mode
        self._mosaic_cache.clear()  # 模式变化，缓存失效
        self.update()
        for m, btn in self.mosaic_mode_buttons.items():
            btn.setProperty("active", m == mode)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _toggle_outline(self):
        self.current_outline = self.outline_toggle.isChecked()
        self.outline_toggle.setProperty("active", self.current_outline)
        self.outline_toggle.style().unpolish(self.outline_toggle)
        self.outline_toggle.style().polish(self.outline_toggle)

    def _select_outline_color(self, color):
        self.current_outline_color = QColor(color)
        for name, btn in self.outline_color_buttons.items():
            btn.setProperty("active", name == color.name())
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _pick_color_at(self, pos):
        """取色器:从背景图 pos 位置吸取像素颜色，设为当前标注色。"""
        bg = self.background_pixmap
        if bg is None or bg.isNull():
            return
        img = bg.toImage()
        # pos 是图像坐标；限制在图内
        x = max(0, min(img.width() - 1, pos.x()))
        y = max(0, min(img.height() - 1, pos.y()))
        c = QColor(img.pixel(x, y))
        if not c.isValid():
            return
        self.current_color = c
        # 同步色板选中态 + 提示
        hex_str = c.name()  # #RRGGBB
        self.setToolTip(f"已取色 {hex_str}")
        # 简易反馈:在状态栏式浮层显示 1 秒
        try:
            from PyQt6.QtWidgets import QLabel
            if not hasattr(self, '_picker_tip'):
                self._picker_tip = QLabel(self)
                self._picker_tip.setStyleSheet(
                    "background-color:rgba(0,0,0,200);color:white;"
                    "padding:4px 10px;border-radius:6px;font-size:12px;")
                self._picker_tip.hide()
            self._picker_tip.setText(f"  {hex_str}  ")
            self._picker_tip.adjustSize()
            self._picker_tip.move(pos.x() + 12, pos.y() + 12)
            self._picker_tip.show()
            QTimer.singleShot(1000, self._picker_tip.hide)
        except Exception:
            pass
        self.update()

    def _create_mosaic_mode_icon(self, mode):
        """马赛克模式图标:block=网格,blur=柔化圆。"""
        pm, p = self._begin_icon()
        if mode == "blur":
            # 柔和模糊:多个半透明同心圆
            for r, a in [(9, 40), (6, 90), (3, 160)]:
                c = QColor(self.ICON_GRAY); c.setAlpha(a)
                p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(10 - r, 10 - r, r * 2, r * 2))
        else:
            # 块状:2x2 网格
            p.setPen(QPen(self.ICON_GRAY_DIM, 0.8))
            p.setBrush(QColor(self.ICON_GRAY))
            for cx, cy in [(5, 5), (15, 5), (5, 15), (15, 15)]:
                p.drawRect(QRectF(cx - 3, cy - 3, 6, 6))
        p.end()
        return QIcon(pm)

    def _select_seq_scale(self, scale):
        """设置序号缩放(小0.85/中1.0/大1.25),仅 sequence 工具用。"""
        self.current_seq_scale = scale
        self._settings.setValue("seq_scale", scale)
        for s, btn in self.seq_scale_buttons.items():
            btn.setProperty("active", s == scale)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _seq_metrics(self, scale=None):
        """序号标注尺寸(按缩放):返回(radius, 数字pt, 文字pt, 文字框高, 水平内边距)。
        基准(中 1.0) = (12, 10, 12, 24, 7);各值向下钳制避免过小。
        文字框高加大(24)与水平内边距(pad_x)共同保证文字与边框四向都有舒适留白。"""
        s = self.current_seq_scale if scale is None else scale
        text_pt = max(6, round(12 * s))
        return (max(6, round(12 * s)),
                max(6, round(10 * s)),
                text_pt,
                max(16, round(24 * s)),
                max(6, round(text_pt * 0.6)))

    def _close_overlays(self):
        """关闭所有浮层子窗口（工具栏、样式面板等）。复制/保存/取消/关闭时调用。"""
        for attr in ('toolbar', 'style_panel'):
            w = getattr(self, attr, None)
            if w is not None:
                w.close()
                w.hide()

    def _copy_to_clipboard(self):
        # 关闭浮层
        self._close_overlays()
        # 生成最终截图（含标注）
        result = self._get_result_pixmap()
        clipboard = QApplication.clipboard()
        clipboard.setPixmap(result)
        self.close()
        self.finished.emit()

    def _cancel_screenshot(self):
        """取消截图 - 关闭工具栏和选区窗口，返回不可截图状态"""
        self._close_overlays()
        # 关闭窗口
        self.close()
        # 发送取消信号，告知 ShortsApp 进入取消状态
        self.cancelled = True
        self.finished.emit()

    def _save_to_file(self):
        from PyQt6.QtWidgets import QFileDialog
        self._close_overlays()
        result = self._get_result_pixmap()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "screenshot.png", "PNG Files (*.png)"
        )
        if file_path:
            result.save(file_path, "PNG")
        self.close()
        self.finished.emit()

    def _get_result_pixmap(self):
        """获取最终截图（含标注）"""
        # 滚动截图：直接基于全分辨率原图渲染。
        # 旧实现先缩小(SmoothTransformation)再放大回原分辨率，是有损的，导致复制/保存后严重模糊。
        # 这里改为用 painter.scale 把"缩放显示"坐标系下的标注映射回全分辨率原图，保持原始清晰度。
        if self.scroll_original_pixmap and not self.scroll_original_pixmap.isNull() \
                and self.selection_rect.isValid():
            result = QPixmap(self.scroll_original_pixmap.copy())
            painter = QPainter(result)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            sf = self.scroll_scale_factor if self.scroll_scale_factor > 0 else 1.0
            painter.scale(sf, sf)
            # 显示坐标系下选区从 (0,0) 起，标注坐标无需额外偏移
            for ann in self.annotations:
                self._draw_annotation(painter, ann, 0, 0)
            painter.end()
            return result

        if self.background_pixmap and self.selection_rect.isValid():
            dpr = self.background_pixmap.devicePixelRatio() or 1.0
            phys_rect = QRect(
                round(self.selection_rect.x() * dpr),
                round(self.selection_rect.y() * dpr),
                round(self.selection_rect.width() * dpr),
                round(self.selection_rect.height() * dpr),
            )
            result = QPixmap(self.background_pixmap.copy(phys_rect))
            result.setDevicePixelRatio(dpr)
            painter = QPainter(result)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            offset_x = self.selection_rect.x()
            offset_y = self.selection_rect.y()

            for ann in self.annotations:
                self._draw_annotation(painter, ann, offset_x, offset_y)

            painter.end()
            return result
        return self.background_pixmap

    def _build_mosaic_block(self, x0, y0, x1, y1, block_size, mode="block"):
        """对背景 [x0,x1)×[y0,y1) 区域做一次像素化/模糊，返回小 pixmap。

        - block(默认):PIL BOX 降采样到块网格再 NEAREST 放大，等价于"每块取平均色"。
        - blur:高斯模糊，radius 按 block_size 映射（对照片类内容更自然）。
        仅在缓存未命中时调用一次。背景图是逻辑像素图（devicePixelRatio=1）。
        """
        bg = self.background_pixmap
        w = x1 - x0
        h = y1 - y0
        if w <= 0 or h <= 0 or bg is None or bg.isNull():
            return QPixmap()
        src = bg.toImage().copy(x0, y0, w, h).convertToFormat(QImage.Format.Format_RGBA8888)
        bits = src.bits()
        bits.setsize(src.sizeInBytes())
        pil = Image.frombytes("RGBA", (w, h), bytes(bits))
        if mode == "blur":
            from PIL import ImageFilter
            radius = max(2, block_size * 0.8)
            pil = pil.filter(ImageFilter.GaussianBlur(radius=radius))
        else:
            cols = max(1, w // block_size)
            rows = max(1, h // block_size)
            pil = pil.resize((cols, rows), Image.BOX).resize((w, h), Image.NEAREST)
        data = pil.tobytes("raw", "RGBA")
        out = QImage(data, w, h, QImage.Format.Format_RGBA8888).copy()
        return QPixmap.fromImage(out)

    def _draw_annotation(self, painter, ann, offset_x=0, offset_y=0):
        """绘制标注"""
        tool = ann.get("tool", "")

        if tool == "sequence":
            # 序号标注
            circle_pos = ann.get("circle_pos")
            text_pos = ann.get("text_pos")
            text = ann.get("text", "")
            number = ann.get("number", 1)
            _seq_scale = ann.get("seq_scale", 1.0)
            radius, _num_pt, _text_pt, text_height, pad_x = self._seq_metrics(_seq_scale)

            if circle_pos and text_pos:
                adj_circle_x = circle_pos.x() - offset_x
                adj_circle_y = circle_pos.y() - offset_y
                adj_text_x = text_pos.x() - offset_x
                adj_text_y = text_pos.y() - offset_y

                color = ann.get("color")
                if color is None:
                    color = QColor(255, 59, 48)

                # 绘制连接线 - 连线终点是文本左边边框的中心
                text_center_y = adj_text_y + text_height / 2
                painter.setPen(QPen(color, 2))
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawLine(int(adj_circle_x), int(adj_circle_y), int(adj_text_x), int(text_center_y))

                # 绘制实心圆圈
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.PenStyle.NoPen)
                rect_circle = QRect(
                    int(adj_circle_x - radius),
                    int(adj_circle_y - radius),
                    radius * 2,
                    radius * 2
                )
                painter.drawEllipse(rect_circle)

                # 在圆圈内绘制数字
                painter.setPen(QPen(Qt.GlobalColor.white))
                font = QFont("Arial", _num_pt, QFont.Weight.Bold)
                painter.setFont(font)
                painter.drawText(rect_circle, int(Qt.AlignmentFlag.AlignCenter), str(number))

                # 绘制文字区域
                if text:
                    fm = QFontMetrics(QFont("Microsoft YaHei", _text_pt))
                    text_width = max(100, fm.horizontalAdvance(text) + pad_x * 2)
                    # 有色边框，透明背景
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(color, 1))
                    painter.drawRect(int(adj_text_x), int(adj_text_y), text_width, text_height)
                    # 与圆圈同色文字
                    font2 = QFont("Microsoft YaHei", _text_pt)
                    painter.setFont(font2)
                    painter.setPen(QPen(color))
                    painter.drawText(
                        QRect(int(adj_text_x + pad_x), int(adj_text_y), text_width - pad_x * 2, text_height),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        text
                    )
            return

        # 其他标注类型
        rect = ann.get("rect")
        if rect is None:
            return

        adjusted_rect = QRect(
            rect.x() - offset_x,
            rect.y() - offset_y,
            rect.width(),
            rect.height()
        )

        if tool == "rectangle":
            if ann.get("outline"):
                painter.setPen(QPen(ann["outline_color"], ann["width"] + 2))
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawRect(adjusted_rect)
            painter.setPen(QPen(ann["color"], ann["width"]))
            self._apply_fill(painter, ann)
            painter.drawRect(adjusted_rect)
        elif tool == "ellipse":
            if ann.get("outline"):
                painter.setPen(QPen(ann["outline_color"], ann["width"] + 2))
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawEllipse(adjusted_rect)
            painter.setPen(QPen(ann["color"], ann["width"]))
            self._apply_fill(painter, ann)
            painter.drawEllipse(adjusted_rect)
        elif tool == "arrow":
            # 箭头 - 使用微信风格填充箭头
            start = ann.get("start_pos")
            end = ann.get("end_pos")
            if start and end:
                # 调整坐标到选区坐标系
                adjusted_start = QPoint(start.x() - offset_x, start.y() - offset_y)
                adjusted_end = QPoint(end.x() - offset_x, end.y() - offset_y)
                self._draw_arrow_head(painter, adjusted_start, adjusted_end, ann["width"], ann["color"])
        elif tool == "line":
            # 直线（可选虚线）
            start = ann.get("start_pos")
            end = ann.get("end_pos")
            if start and end:
                adjusted_start = QPoint(start.x() - offset_x, start.y() - offset_y)
                adjusted_end = QPoint(end.x() - offset_x, end.y() - offset_y)
                style = Qt.PenStyle.DashLine if ann.get("dashed") else Qt.PenStyle.SolidLine
                if ann.get("outline"):
                    op = QPen(ann["outline_color"], ann["width"] + 2, style)
                    op.setCapStyle(Qt.PenCapStyle.RoundCap)
                    painter.setPen(op)
                    painter.drawLine(adjusted_start, adjusted_end)
                pen = QPen(ann["color"], ann["width"], style)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(adjusted_start, adjusted_end)
        elif tool == "text":
            # 文字标注 - 使用与输入时相同的字体大小
            font_size = ann.get("width", 12)
            font = QFont("Microsoft YaHei", font_size)
            painter.setFont(font)
            text = ann.get("text", "文字")
            tx = adjusted_rect.x()
            ty = adjusted_rect.y() + font_size
            if ann.get("outline"):
                # 文字描边:用 path 描边一层描边色，再画本色文字
                path = QPainterPath()
                path.addText(QPointF(tx, ty), font, text)
                painter.setPen(QPen(ann["outline_color"], max(2, font_size // 4)))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
            painter.setPen(QPen(ann["color"]))
            painter.drawText(tx, ty, text)
        elif tool == "pen":
            # 画笔 - 绘制自由线条
            painter.setPen(QPen(ann["color"], ann["width"]))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            points = ann.get("points", [])
            if len(points) > 1:
                for i in range(len(points) - 1):
                    # 调整坐标
                    p1 = QPoint(points[i].x() - offset_x, points[i].y() - offset_y)
                    p2 = QPoint(points[i + 1].x() - offset_x, points[i + 1].y() - offset_y)
                    painter.drawLine(p1, p2)
        elif tool == "highlight":
            # 高亮 - 半透明矩形
            highlight_color = QColor(ann["color"])
            highlight_color.setAlpha(80)  # 半透明
            painter.setBrush(QBrush(highlight_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(adjusted_rect)
        elif tool == "mosaic":
            # 真正的马赛克效果：像素化背景区域。
            # 旧实现每次重绘都 toImage() 全屏拷贝 + 逐像素 img.pixel()
            # (O(块面积) 的 Python 循环 × 每帧)，大区域/多块时严重卡顿。
            # 现改为按 (ann, 区域, 背景版本) 缓存一张像素化 pixmap，命中即直接 drawPixmap。
            if self.background_pixmap and not self.background_pixmap.isNull():
                block_size = ann.get("mosaic_size", 10)
                mode = ann.get("mosaic_mode", "block")
                x0 = max(0, rect.x())
                y0 = max(0, rect.y())
                x1 = min(rect.right() + 1, self.background_pixmap.width())
                y1 = min(rect.bottom() + 1, self.background_pixmap.height())
                if x1 <= x0 or y1 <= y0:
                    return
                key = (id(ann), x0, y0, x1, y1, block_size, mode, self._bg_version)
                block = self._mosaic_cache.get(key)
                if block is None:
                    block = self._build_mosaic_block(x0, y0, x1, y1, block_size, mode)
                    # 淘汰同一标注的旧缓存(矩形/背景变化后)，避免堆积
                    aid = id(ann)
                    self._mosaic_cache = {
                        k: v for k, v in self._mosaic_cache.items()
                        if k[0] != aid or k == key
                    }
                    self._mosaic_cache[key] = block
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawPixmap(int(x0 - offset_x), int(y0 - offset_y), block)
        elif tool == "sequence":
            # 序号标注 - 实心圆圈 + 连接线 + 文字
            circle_pos = ann.get("circle_pos")
            radius = ann.get("circle_radius", 12)
            text_pos = ann.get("text_pos")
            text = ann.get("text", "")
            number = ann.get("number", 1)

            if circle_pos and text_pos:
                # 调整坐标到选区坐标系
                adj_circle_x = circle_pos.x() - offset_x
                adj_circle_y = circle_pos.y() - offset_y
                adj_text_x = text_pos.x() - offset_x
                adj_text_y = text_pos.y() - offset_y

                color = ann.get("color")
                if color is None:
                    color = QColor(255, 59, 48)

                # 绘制连接线 - 连线终点是文本左边边框的中心
                text_height = 20
                text_center_y = adj_text_y + text_height / 2
                painter.setPen(QPen(color, 2))
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawLine(int(adj_circle_x), int(adj_circle_y), int(adj_text_x), int(text_center_y))

                # 绘制实心圆圈
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.PenStyle.NoPen)
                rect_circle = QRect(
                    int(adj_circle_x - radius),
                    int(adj_circle_y - radius),
                    radius * 2,
                    radius * 2
                )
                painter.drawEllipse(rect_circle)

                # 在圆圈内绘制数字
                painter.setPen(QPen(Qt.GlobalColor.white))
                font = QFont("Arial", 10, QFont.Weight.Bold)
                painter.setFont(font)
                painter.drawText(rect_circle, int(Qt.AlignmentFlag.AlignCenter), str(number))

                # 绘制文字区域（只有不在编辑中时才绘制，避免重影）
                is_editing = hasattr(self, 'current_sequence_ann') and self.current_sequence_ann is ann
                if text and not is_editing:
                    seq_font = QFont("Microsoft YaHei", 12)
                    fm = QFontMetrics(seq_font)
                    text_width = max(100, fm.horizontalAdvance(text) + 10)
                    text_height = 20
                    # 文字背景
                    bg_color = QColor(color)
                    bg_color.setAlpha(180)
                    painter.setBrush(QBrush(bg_color))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRect(int(adj_text_x), int(adj_text_y), text_width, text_height)
                    # 文字
                    painter.setFont(seq_font)
                    painter.setPen(QPen(Qt.GlobalColor.white))
                    text_rect = QRect(int(adj_text_x), int(adj_text_y), text_width, text_height)
                    painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

    def _draw_arrow_head(self, painter, start, end, line_width=None, color=None):
        """绘制微信风格箭头头部（带内凹设计）"""
        import math

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        if dx == 0 and dy == 0:
            return

        length = math.hypot(dx, dy)
        if length < 10:
            return

        angle = math.atan2(dy, dx)
        sin_a = math.sin(angle)
        cos_a = math.cos(angle)

        if line_width is None:
            line_width = 4
        if color is None:
            color = QColor(255, 59, 48)

        w = max(line_width, 1.0)

        # 微信风格箭头尺寸计算(头部随线宽等比缩放;倍数过大曾导致箭头偏大)
        head_len = min(length * 0.35, 6 * w)    # 箭头头部长度(默认 w=4≈24px)
        head_hw = head_len * 0.5                 # 翼展半宽
        stem_hw = head_hw * 0.25                 # 箭柄半宽

        # 翼展基线
        base_x = end.x() - head_len * cos_a
        base_y = end.y() - head_len * sin_a

        # 内凹偏移
        notch_fwd = head_len * 0.3
        notch_x = base_x + notch_fwd * cos_a
        notch_y = base_y + notch_fwd * sin_a

        # 6个顶点
        points = [
            # 1. 尾部尖点
            QPointF(start.x(), start.y()),
            # 2. 箭柄右侧 → 内凹点
            QPointF(notch_x + stem_hw * sin_a, notch_y - stem_hw * cos_a),
            # 3. 右翼尖端
            QPointF(base_x + head_hw * sin_a, base_y - head_hw * cos_a),
            # 4. 箭尖
            QPointF(end.x(), end.y()),
            # 5. 左翼尖端
            QPointF(base_x - head_hw * sin_a, base_y + head_hw * cos_a),
            # 6. 箭柄左侧 → 内凹点
            QPointF(notch_x - stem_hw * sin_a, notch_y + stem_hw * cos_a),
        ]

        # 绘制填充的箭头
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))

        path = QPainterPath()
        path.moveTo(points[0])
        for p in points[1:]:
            path.lineTo(p)
        path.closeSubpath()

        painter.drawPath(path)

    def wheelEvent(self, event):
        """滚动结果视图中：滚轮缩放(以光标为中心)；其它模式忽略。"""
        if not self._is_scroll_result():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        # 缩放前先收起进行中的文字输入，避免输入框位置/字号错位
        if getattr(self, 'text_input', None):
            self._finish_text_input()
        old_zoom = self.view_zoom
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_zoom = max(0.1, min(8.0, old_zoom * factor))
        if new_zoom == old_zoom:
            return
        cursor = event.position()  # 屏幕坐标(QPointF)
        # 保持光标下同一图像点不动
        img_x = (cursor.x() - self.view_offset.x()) / old_zoom
        img_y = (cursor.y() - self.view_offset.y()) / old_zoom
        self.view_zoom = new_zoom
        self.view_offset = QPointF(
            cursor.x() - img_x * new_zoom,
            cursor.y() - img_y * new_zoom,
        )
        self.update()

    def _start_scroll_capture(self):
        """启动滚动截图模式"""
        self.is_scroll_capturing = True
        self.scroll_capture_rect = QRect(self.selection_rect)  # 保存屏幕坐标
        self.scroll_frames = []
        self.scroll_no_change_count = 0
        self.scroll_last_bytes = None
        self.scroll_last_sample = None
        self.scroll_stable_count = 0

        # 隐藏自身 + 工具栏 + 样式面板（滚动模式不需要标注工具）
        if hasattr(self, 'toolbar'):
            self.toolbar.hide()
        if hasattr(self, 'style_panel'):
            self.style_panel.hide()
        self.hide()

        # 显示捕获区域边框(透明内部，不抢焦点，不进入捕获区)
        self._show_scroll_capture_outline()
        # 显示浮窗(不抢焦点)
        self._show_scroll_capture_bar()

        # macOS：隐藏后 Shorts 仍是 frontmost，用户滚轮送不到被截应用。
        # 把焦点/激活状态交给最前面的目标应用，让滚动直达它（这样 _capture_scroll_frame
        # 才能抓到滚动的帧）。必须在启动抓帧定时器之前完成。
        activate_foreground_app()

        # 激活目标应用后，我们的浮窗可能被压在目标窗口之下。延迟把"完成"
        # 浮窗提到 NSStatusWindowLevel（高于普通窗口与 Dock），保证可见可点。
        # 注意：scroll_capture_outline（全屏遮罩）不能 raise——它在最高层级会
        # 拦截滚轮事件，导致目标窗口滚不动（WA_TransparentForMouseEvents 在
        # NSStatusWindowLevel 下对滚轮不可靠）。遮罩留在普通 Tool 层级即可。
        def _raise_overlays():
            if getattr(self, 'scroll_capture_bar', None):
                raise_overlay(self.scroll_capture_bar)
        QTimer.singleShot(250, _raise_overlays)

        # 延迟启动 Timer(给窗口管理器时间处理隐藏)
        QTimer.singleShot(400, self._start_scroll_timer)

    def _show_scroll_capture_outline(self):
        """显示捕获区域的高亮遮罩(选区外遮罩 + 蓝边)，复刻正常选区高亮模式。

        遮罩透明且不抢事件，捕获区内部无任何绘制，保证帧画面干净。
        属性名沿用 scroll_capture_outline，_finish_scroll_capture 的清理逻辑无需改动。
        """
        self.scroll_capture_outline = _ScrollDimOverlay(self.scroll_capture_rect)
        self.scroll_capture_outline.show()
        # macOS：全屏遮罩必须彻底忽略鼠标（含滚轮），否则处于高层级时会拦截
        # 用户滚轮，导致目标窗口滚不动。原生 setIgnoresMouseEvents 比 Qt 的
        # WA_TransparentForMouseEvents 对滚轮更可靠。
        make_mouse_passthrough(self.scroll_capture_outline)

    def _show_scroll_capture_bar(self):
        """显示滚动截图浮窗"""
        self.scroll_capture_bar = QFrame()
        self.scroll_capture_bar.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.scroll_capture_bar.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating
        )
        self.scroll_capture_bar.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self.scroll_capture_bar.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground
        )
        self.scroll_capture_bar.setFixedSize(280, 40)
        self.scroll_capture_bar.setStyleSheet("""
            QFrame {
                background-color: rgba(35, 35, 35, 240);
                border-radius: 8px;
            }
            QLabel {
                color: white;
                font-size: 12px;
            }
            QPushButton {
                background-color: #0a84ff;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #0070e0;
            }
        """)

        layout = QHBoxLayout(self.scroll_capture_bar)
        layout.setContentsMargins(12, 4, 12, 4)
        self.scroll_status_label = QLabel("滚动截图中 | 已捕获 0 帧（自由滚动，自动抓帧）")
        done_btn = QPushButton("完成")
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.clicked.connect(self._finish_scroll_capture)
        layout.addWidget(self.scroll_status_label)
        layout.addStretch()
        layout.addWidget(done_btn)

        # 定位到"捕获区域所在屏幕"的顶部居中(多显示器下跟随用户当前屏幕)
        scr = QGuiApplication.screenAt(self.scroll_capture_rect.center()) or QApplication.primaryScreen()
        sg = scr.geometry()
        x = sg.left() + (sg.width() - 280) // 2
        self.scroll_capture_bar.move(x, sg.top() + 20)
        self.scroll_capture_bar.show()

    def _start_scroll_timer(self):
        """启动子线程高频抓帧。

        关键：抓帧要密(~40ms 一帧)且不能阻塞主线程——主线程若被 mss.grab
        占满，目标窗口收不到滚轮事件(就是之前"无法滚动"的原因)。故把抓帧
        放到 QThread，主线程只接收信号做轻量去重/保存。子线程抓帧期间，用户
        的滚动正常送达目标窗口，且相邻帧重叠充分，NCC 拼接可靠。
        """
        self._scroll_worker = _ScrollCaptureWorker(self.scroll_capture_rect)
        self._scroll_thread = QThread()
        self._scroll_worker.moveToThread(self._scroll_thread)
        self._scroll_thread.started.connect(self._scroll_worker.run)
        self._scroll_worker.frame_captured.connect(self._on_scroll_frame_captured)
        self._scroll_thread.start()

    def _on_scroll_frame_captured(self, data, x, y, w, h):
        """主线程：接收子线程抓到的帧，做去重/保存。"""
        last_saved = self.scroll_last_bytes
        if last_saved is not None and len(last_saved) == len(data):
            diff = _bytes_diff_ratio(last_saved, data)
            if diff < 0.015:  # 变化 <2% 视为未显著滚动，跳过
                # 内容未显著变化 → 累计静止次数
                self.scroll_no_change_count += 1
                if self.scroll_no_change_count >= 40:  # ~静止 1.6s → 自动结束
                    self._finish_scroll_capture()
                return
        # 有变化 → 保存(转 QPixmap 供后续拼接)
        img = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        self.scroll_frames.append(QPixmap.fromImage(img))
        self.scroll_last_bytes = data
        self.scroll_no_change_count = 0
        if self.scroll_status_label:
            self.scroll_status_label.setText(
                f"滚动截图中 | 已捕获 {len(self.scroll_frames)} 帧"
            )

    def _run_ocr(self):
        """OCR 识别选区文字：对当前选区图做识别，浮层显示结果并支持复制。"""
        if not self.selection_rect.isValid():
            return
        if not is_macos():
            print("OCR 仅 macOS 支持")
            return
        # 取选区图（含标注前的背景）
        bg = self.background_pixmap
        if bg is None or bg.isNull():
            return
        sub = bg.copy(self.selection_rect)
        # 临时提示
        tip = self._show_float_tip("识别中…")
        QApplication.processEvents()
        try:
            from core.ocr import recognize
            results = recognize(sub)
        except Exception as e:
            self._show_float_tip(f"OCR 失败: {e}", 2000)
            return
        if tip is not None:
            tip.close()
        if not results:
            self._show_float_tip("未识别到文字", 1500)
            return
        # 全部文字拼一起，复制到剪贴板
        all_text = "\n".join(r["text"] for r in results)
        QApplication.clipboard().setText(all_text)
        self._show_float_tip(f"识别到 {len(results)} 块，已复制到剪贴板", 2500)

    def _show_float_tip(self, text, msec=1200):
        """在选区中心显示一个临时浮层提示，返回 QLabel 引用。"""
        from PyQt6.QtWidgets import QLabel
        lbl = QLabel(text, self)
        lbl.setStyleSheet(
            "background-color:rgba(0,0,0,200);color:white;padding:8px 14px;"
            "border-radius:8px;font-size:13px;")
        lbl.adjustSize()
        cx = self.selection_rect.center().x() - lbl.width() // 2
        cy = self.selection_rect.center().y() - lbl.height() // 2
        lbl.move(cx, cy)
        lbl.show()
        lbl.raise_()
        if msec > 0:
            QTimer.singleShot(msec, lbl.close)
        return lbl

    def _start_gif_record(self):
        """开始录制 GIF：隐藏选区窗口 + 工具栏，显示录制浮窗，子线程抓帧。"""
        try:
            import pathlib
            pathlib.Path("/tmp/gif_debug.log").write_text(
                f"start_gif: rect={self.selection_rect}\n", encoding="utf-8")
        except Exception:
            pass
        if not self.selection_rect.isValid():
            return
        from core.gif_recorder import GifRecorder
        self._gif_rect = QRect(self.selection_rect)
        self._gif_frames = []
        # 显示点击效果开关（默认开）
        self._gif_show_clicks = True
        self._gif_outline = None
        # 隐藏自身 + 浮层
        self._close_overlays()
        self.hide()
        try:
            # 显示选区高亮遮罩
            self._gif_outline = _ScrollDimOverlay(self._gif_rect)
            self._gif_outline.show()
            make_mouse_passthrough(self._gif_outline)
            make_floating_panel(self._gif_outline)
            # 先创建 recorder
            self._gif_recorder = GifRecorder(self._gif_rect, fps=12, show_clicks=self._gif_show_clicks)
            self._gif_recorder.frame_captured.connect(self._on_gif_frame)
            # 透明标注覆盖层（必须在 _make_record_bar 之前创建，bar 的按钮要引用它）
            self._gif_ann_overlay = _GifAnnotationOverlay(
                self._gif_rect, self._gif_recorder)
            self._gif_ann_overlay._on_ann_added = self._on_gif_ann_added
            self._gif_ann_overlay.show()
            make_floating_panel(self._gif_ann_overlay)
            # 录制浮窗（含标注工具按钮，引用 overlay）
            self._gif_bar = self._make_record_bar()
            self._gif_bar.show()
        except Exception as e:
            import traceback
            try:
                import pathlib
                pathlib.Path("/tmp/gif_debug.log").write_text(
                    f"EXCEPTION: {e}\n{traceback.format_exc()}", encoding="utf-8")
            except Exception:
                pass
            print(f"GIF 录制启动失败: {e}")
            traceback.print_exc()
            self.show()
            return
        # 让出焦点
        activate_foreground_app()
        # 启动抓帧
        self._gif_recorder.start()
        QTimer.singleShot(300, self._start_gif_raise_loop)
        try:
            import pathlib
            p = pathlib.Path("/tmp/gif_debug.log")
            prev = p.read_text(encoding="utf-8") if p.exists() else ""
            p.write_text(prev + f"OK bar_pos=({self._gif_bar.x()},{self._gif_bar.y()})\n", encoding="utf-8")
        except Exception:
            pass

    def _start_gif_raise_loop(self):
        """初始设浮动面板 + 启动高频保持置顶。"""
        bar = getattr(self, "_gif_bar", None)
        if bar is not None and bar.isVisible():
            # 关键：设成 non-activating floating panel（始终浮在其他 app 之上）
            make_floating_panel(bar)
            self._raise_gif_bar()
        self._gif_raise_timer = QTimer(self)
        self._gif_raise_timer.timeout.connect(self._raise_gif_bar)
        self._gif_raise_timer.start(100)  # 每 100ms 重新提到最前

    def _raise_gif_bar(self):
        """把选区遮罩 + 录制浮窗强制提到所有窗口之上（遮罩先、面板后）。"""
        bar = getattr(self, "_gif_bar", None)
        outline = getattr(self, "_gif_outline", None)
        if is_macos():
            try:
                import objc
                from PyQt6.QtGui import QGuiApplication
                if QGuiApplication.platformName() == "cocoa":
                    # 先 raise 遮罩（底层），再 raise 面板（浮在遮罩之上）
                    for w in (outline, getattr(self, "_gif_ann_overlay", None), bar):
                        if w is None or not w.isVisible():
                            continue
                        wid = int(w.winId())
                        if wid:
                            view = objc.objc_object(c_void_p=wid)
                            ns_win = view.window()
                            if ns_win is not None:
                                ns_win.orderFrontRegardless()
                    return
            except Exception:
                pass
        if outline is not None and outline.isVisible():
            outline.raise_()
        if bar is not None and bar.isVisible():
            bar.raise_()

    def _set_gif_ann_color(self, color):
        """设置 GIF 标注颜色。"""
        ov = getattr(self, "_gif_ann_overlay", None)
        if ov is not None:
            ov._color = QColor(color)

    def _on_gif_ann_added(self):
        """标注画完后：状态面板显示"闪烁中"提示，1.8 秒后恢复。"""
        if self._gif_status:
            self._gif_status.setText("⏳ 标注闪烁中…请稍候")
            self._gif_status.setStyleSheet("color:#ffcc00; font-size:12px;")
        QTimer.singleShot(1800, self._restore_gif_status)

    def _restore_gif_status(self):
        """恢复录制状态面板的帧数显示。"""
        if self._gif_status:
            count = len(getattr(self, "_gif_frame_files", []))
            secs = count / 12
            self._gif_status.setText(f"● 录制中 {secs:.0f}s ({count} 帧)")
            self._gif_status.setStyleSheet("color:#ff453a; font-size:12px;")

    def _make_record_bar(self):
        """录制中的浮窗（停止按钮 + 帧数 + 显示点击开关）。"""
        bar = QFrame()
        bar.setWindowFlags(overlay_window_flags())
        bar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        bar.setFixedHeight(40)
        bar.setStyleSheet("""
            QFrame { background-color: rgba(35,35,35,240); border-radius:8px; }
            QLabel { color:#ff453a; font-size:12px; }
            QPushButton { background-color:transparent; color:#ddd; border:none;
                border-radius:4px; padding:4px 8px; font-size:11px; }
            QPushButton:hover { background-color:rgba(255,255,255,32); }
            QPushButton[active="true"] { background-color:#0a84ff; color:white; }
        """)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 4, 12, 4)
        self._gif_status = QLabel("● 录制中 0 帧")
        lay.addWidget(self._gif_status)
        lay.addStretch()

        # 标注工具按钮
        overlay = self._gif_ann_overlay
        rect_btn = QPushButton("▭")
        rect_btn.setToolTip("画矩形标注（闪烁后自动消失）")
        rect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rect_btn.clicked.connect(lambda: overlay.set_tool("rect"))
        lay.addWidget(rect_btn)

        arrow_btn = QPushButton("→")
        arrow_btn.setToolTip("画箭头标注（闪烁后自动消失）")
        arrow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        arrow_btn.clicked.connect(lambda: overlay.set_tool("arrow"))
        lay.addWidget(arrow_btn)

        # 颜色选择（红/黄/绿 三色快速切换）
        for c, name in [(QColor(255,59,48), "红"), (QColor(255,200,0), "黄"), (QColor(50,200,50), "绿")]:
            cb = QPushButton()
            cb.setFixedSize(16, 16)
            cb.setStyleSheet(f"background-color:{c.name()};border-radius:8px;border:1px solid #888;")
            cb.setToolTip(f"{name}色")
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.clicked.connect(lambda checked, col=c: self._set_gif_ann_color(col))
            lay.addWidget(cb)

        # 分隔
        sep = QFrame()
        sep.setFixedSize(1, 24)
        sep.setStyleSheet("background-color:#555;")
        lay.addWidget(sep)

        stop_btn = QPushButton("停止")
        stop_btn.setStyleSheet("background-color:#ff453a;color:white;")
        stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        stop_btn.clicked.connect(self._stop_gif_record)
        lay.addWidget(stop_btn)

        bar.adjustSize()
        # 自适应定位：紧贴选区边缘，不遮挡录制内容
        bar_w = bar.width()
        bar_h = 40
        gap = 8  # 与选区边缘的间距
        r = self._gif_rect  # 选区矩形（屏幕坐标）
        scr = QGuiApplication.screenAt(r.center()) or QApplication.primaryScreen()
        sg = scr.availableGeometry()  # 可用区域（排除菜单栏/Dock）
        # 水平：选区水平居中
        x = r.center().x() - bar_w // 2
        # 垂直优先级：
        # 1) 选区顶部外侧上方（选区下方有内容可录时不遮挡）
        y = r.top() - bar_h - gap
        if y < sg.top():
            # 2) 顶部空间不够 → 放选区底部外侧下方
            y = r.bottom() + gap
            if y + bar_h > sg.bottom():
                # 3) 底部也不够 → 放选区内部底部（贴边）
                y = r.bottom() - bar_h - gap
        # 水平越界修正
        x = max(sg.left(), min(x, sg.right() - bar_w))
        bar.move(x, y)
        return bar

    def _toggle_gif_clicks(self, state):
        """切换"显示点击"效果。"""
        on = bool(state)
        self._gif_show_clicks = on
        rec = getattr(self, "_gif_recorder", None)
        if rec is not None:
            rec._show_clicks = on
            if not on:
                with rec._clicks_lock:
                    rec._clicks.clear()

    def _on_gif_frame(self, pm):
        """收到一帧 QPixmap：写 PNG 到临时目录（不存内存，避免内存爆炸）。"""
        if not hasattr(self, "_gif_tmpdir") or self._gif_tmpdir is None:
            import tempfile
            self._gif_tmpdir = tempfile.mkdtemp(prefix="shorts_gif_")
        idx = len(self._gif_frame_files) if hasattr(self, "_gif_frame_files") else 0
        if not hasattr(self, "_gif_frame_files"):
            self._gif_frame_files = []
        path = f"{self._gif_tmpdir}/frame_{idx:05d}.png"
        pm.save(path, "PNG")
        self._gif_frame_files.append(path)
        if self._gif_status:
            secs = len(self._gif_frame_files) / 12
            self._gif_status.setText(f"● 录制中 {secs:.0f}s ({len(self._gif_frame_files)} 帧)")
        # 不再限制 360 帧（磁盘存储，内存不爆炸）。限制 5 分钟 = 3600 帧
        if len(self._gif_frame_files) >= 3600:
            self._stop_gif_record()

    def _stop_gif_record(self):
        """停止录制，选格式保存（GIF / MP4）。"""
        timer = getattr(self, "_gif_raise_timer", None)
        if timer is not None:
            timer.stop()
            self._gif_raise_timer = None
        rec = getattr(self, "_gif_recorder", None)
        if rec is not None:
            rec.stop()
            rec.stop_tap()
        outline = getattr(self, "_gif_outline", None)
        if outline is not None:
            outline.close()
            outline.deleteLater()
            self._gif_outline = None
        ann_ov = getattr(self, "_gif_ann_overlay", None)
        if ann_ov is not None:
            ann_ov.close()
            ann_ov.deleteLater()
            self._gif_ann_overlay = None
        if self._gif_bar:
            self._gif_bar.close()
            self._gif_bar.deleteLater()
            self._gif_bar = None
        frame_files = getattr(self, "_gif_frame_files", [])
        if not frame_files:
            self.show()
            self._show_toolbar()
            return
        tip = self._show_float_tip("选择保存格式…", 0)
        QApplication.processEvents()
        try:
            from PyQt6.QtWidgets import QFileDialog, QMessageBox
            # 先让用户选格式
            fmt_box = QMessageBox(self)
            fmt_box.setWindowTitle("保存录制")
            secs = len(frame_files) / 12
            fmt_box.setText(f"录制完成：{len(frame_files)} 帧（{secs:.0f} 秒）\n选择保存格式：")
            gif_btn = fmt_box.addButton("GIF", QMessageBox.ButtonRole.AcceptRole)
            mp4_btn = fmt_box.addButton("MP4（体积更小）", QMessageBox.ButtonRole.AcceptRole)
            cancel_btn = fmt_box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            fmt_box.exec()
            clicked = fmt_box.clickedButton()
            if clicked == cancel_btn or clicked is None:
                if tip is not None:
                    tip.close()
                self._cleanup_gif_tmp()
                self._gif_frame_files = []
                self.close()
                self.finished.emit()
                return
            use_mp4 = (clicked == mp4_btn)
            default_name = "recording.mp4" if use_mp4 else "recording.gif"
            file_path, _ = QFileDialog.getSaveFileName(
                self, "保存", default_name,
                "MP4 (*.mp4)" if use_mp4 else "GIF (*.gif)")
            if not file_path:
                if tip is not None:
                    tip.close()
                self._cleanup_gif_tmp()
                self._gif_frame_files = []
                self.close()
                self.finished.emit()
                return
            if use_mp4:
                import subprocess
                tmpdir = self._gif_tmpdir
                # 用完整路径找 ffmpeg（.app 环境 PATH 可能不同）
                import shutil as _sh
                ffmpeg_path = _sh.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg" or "/usr/local/bin/ffmpeg"
                cmd = [ffmpeg_path, "-y", "-framerate", "12",
                       "-i", f"{tmpdir}/frame_%05d.png",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p",
                       "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                       file_path]
                # 进度对话框
                from PyQt6.QtWidgets import QProgressDialog
                prog = QProgressDialog("正在生成 MP4…", "取消", 0, 100, self)
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setAutoClose(False)
                prog.setValue(10)
                QApplication.processEvents()
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                # 读 stderr 的 frame= 进度（ffmpeg 输出到 stderr）
                import re
                while proc.poll() is None:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    m = re.search(r'frame=\s*(\d+)', line)
                    if m:
                        done = int(m.group(1))
                        pct = min(99, int(done / max(1, len(frame_files)) * 100))
                        prog.setValue(pct)
                        QApplication.processEvents()
                proc.wait(timeout=60)
                prog.setValue(100)
                prog.close()
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    self._show_float_tip(f"已保存 MP4：{file_path}", 2500)
                else:
                    err = proc.stderr.read()[-200:] if proc.stderr else ""
                    self._show_float_tip(f"MP4 失败：{err}", 3000)
            else:
                tip.setText("  正在生成 GIF…  ")
                QApplication.processEvents()
                # GIF 生成也加进度
                from PyQt6.QtWidgets import QProgressDialog
                prog = QProgressDialog("正在生成 GIF…", None, 0, 100, self)
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(20)
                QApplication.processEvents()
                from core.gif_recorder import frames_from_files_to_gif
                ok = frames_from_files_to_gif(frame_files, file_path, fps=12)
                prog.setValue(100)
                prog.close()
                if ok:
                    self._show_float_tip(f"已保存 GIF：{file_path}", 2500)
                else:
                    self._show_float_tip("GIF 生成失败", 2500)
        except Exception as e:
            self._show_float_tip(f"生成失败: {e}", 2500)
        if tip is not None:
            tip.close()
        # 清理临时文件
        self._cleanup_gif_tmp()
        self._gif_frame_files = []
        self.close()
        self.finished.emit()

    def _cleanup_gif_tmp(self):
        """清理 GIF 录制的临时 PNG 文件。"""
        import shutil
        tmpdir = getattr(self, "_gif_tmpdir", None)
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            self._gif_tmpdir = None
        self._gif_frames = []
        self.close()
        self.finished.emit()

    def _finish_scroll_capture(self):
        """结束滚动截图：在主线程同步拼接。

        历史上这里用 QThread + _StitchWorker 把 _stitch_images 放到工作线程，
        但在 Python3.14 + PyQt6 下该模式会触发原生崩溃：worker.finished 的第 1
        个槽 _on_stitch_finished 运行时线程尚未 quit，其间任何释放都会"删除运
        行中的 QThread"(Qt 文档明示这会崩溃)；线程 deleteLater/解释器退出时的
        GC 也存在 use-after-free。_stitch_images 是纯函数(仅对 QImage/字节做
        计算，不碰 QPixmap)，通常亚秒级，故回到主线程同步拼接——短暂冻结 UI
        远好于必崩。先弹出"拼接中"并强制重绘，让用户看到提示后再阻塞计算。
        """
        # 停止抓帧工作线程
        worker = getattr(self, '_scroll_worker', None)
        thread = getattr(self, '_scroll_thread', None)
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            thread.wait(1500)
        self._scroll_worker = None
        self._scroll_thread = None
        if self.scroll_timer:
            self.scroll_timer.stop()
            self.scroll_timer = None

        # 隐藏浮窗
        if self.scroll_capture_bar:
            self.scroll_capture_bar.close()
            self.scroll_capture_bar.deleteLater()
            self.scroll_capture_bar = None

        # 隐藏边框
        if self.scroll_capture_outline:
            self.scroll_capture_outline.close()
            self.scroll_capture_outline.deleteLater()
            self.scroll_capture_outline = None

        self.is_scroll_capturing = False

        frames = self.scroll_frames
        if not frames:
            # 没有任何帧，直接恢复
            self.show()
            if hasattr(self, 'toolbar'):
                self._show_toolbar()
            return

        # 显示"拼接中"浮窗并强制立即重绘(随后的同步拼接会阻塞事件循环)
        self._stitch_progress = QLabel("正在拼接…")
        self._stitch_progress.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self._stitch_progress.setStyleSheet(
            "QLabel{background-color:rgba(35,35,35,240);color:white;"
            "font-size:12px;padding:10px 18px;border-radius:8px;}"
        )
        self._stitch_progress.adjustSize()
        scr = QGuiApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        sg = scr.geometry()
        self._stitch_progress.move(
            sg.left() + (sg.width() - self._stitch_progress.width()) // 2,
            sg.top() + (sg.height() - self._stitch_progress.height()) // 2,
        )
        self._stitch_progress.show()
        self._stitch_progress.repaint()  # 强制立即绘制，确保提示在阻塞计算前可见

        # 主线程同步拼接(_stitch_images 是纯 QImage 计算，无 QPixmap/线程依赖)
        images = [pm.toImage() for pm in frames]
        result = _stitch_images(images)
        self._on_stitch_finished(result)

    def _on_stitch_finished(self, image):
        """拼接完成：把结果图切到滚动结果视图。

        当前为同步实现——_finish_scroll_capture 在主线程里直接调用本方法，
        不存在工作线程/引用回收问题。仅清理"拼接中"浮窗，然后切换到滚动
        结果视图(必要时按屏幕高度缩放原图，并复位标注/撤销/视图状态)。
        """
        # 清理进度浮窗
        prog = getattr(self, "_stitch_progress", None)
        if prog is not None:
            prog.close()
            prog.deleteLater()
            self._stitch_progress = None

        stitched = None
        if image is not None and not image.isNull():
            stitched = QPixmap.fromImage(image)
            dpr = device_pixel_ratio()
            if dpr != 1.0:
                stitched.setDevicePixelRatio(dpr)

        if stitched and not stitched.isNull():
            # 保存全分辨率原图
            self.scroll_original_pixmap = stitched

            # 缩放适配屏幕高度
            screen_h = QApplication.primaryScreen().geometry().height()
            if stitched.height() > screen_h:
                scaled = stitched.scaledToHeight(
                    screen_h,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.scroll_scale_factor = stitched.height() / scaled.height()
            else:
                scaled = stitched
                self.scroll_scale_factor = 1.0

            # 替换背景图(失效马赛克缓存)
            self.background_pixmap = scaled
            self._bg_version += 1
            self._mosaic_cache.clear()
            # selection_rect 覆盖整个缩放后区域
            self.selection_rect = QRect(0, 0, scaled.width(), scaled.height())
            # 清除已有标注
            self.annotations = []
            self.selected_ann = None
            self._reset_undo()

            # 初始化滚动结果视图：100% 缩放，整图在屏幕中居中
            screen = QApplication.primaryScreen().geometry()
            self.view_zoom = 1.0
            ox = (screen.width() - scaled.width()) / 2.0
            oy = (screen.height() - scaled.height()) / 2.0
            self.view_offset = QPointF(max(0.0, ox), max(0.0, oy))

        # 恢复显示
        self.show()
        if hasattr(self, 'toolbar'):
            self._show_toolbar()
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._handle_escape()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.selection_rect.isValid():
                self._copy_to_clipboard()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # 删除选中的标注(文字输入中不拦截，交给 QLineEdit)
            if self.selection_confirmed and not getattr(self, 'text_input', None) \
                    and self.selected_ann is not None:
                self.annotations = [a for a in self.annotations if a is not self.selected_ann]
                self.selected_ann = None
                self._commit_undo()
                self.update()
        elif (event.modifiers() & Qt.KeyboardModifier.ControlModifier) \
                and event.key() in (Qt.Key.Key_Z, Qt.Key.Key_Y):
            # 撤销 Ctrl+Z / 重做 Ctrl+Y 或 Ctrl+Shift+Z
            if self.selection_confirmed and not getattr(self, 'text_input', None):
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if event.key() == Qt.Key.Key_Y or (event.key() == Qt.Key.Key_Z and shift):
                    self._redo()
                else:
                    self._undo()

    def mouseDoubleClickEvent(self, event):
        """双击序号标注的文字区域进入编辑模式"""
        if not self.selection_confirmed:
            return

        pos = self._img_pos(event.pos())
        hit_result = self._hit_test_annotation(pos)
        if hit_result:
            ann, hit_type = hit_result
            if hit_type == "sequence_text":
                self._edit_sequence_text(ann)
            elif hit_type in ("text_edit", "text_border"):
                self._edit_text_annotation(ann, hit_type)


class AnnotationWindow(QWidget):
    """标注窗口 - 沉浸式体验，类似iShot"""

    def __init__(self, pixmap):
        super().__init__()
        self.pixmap = pixmap
        self.annotations = []
        self.current_tool = "arrow"
        self.current_color = QColor(255, 59, 48)
        self.current_width = 4
        self.is_drawing = False
        self.start_point = QPoint()
        self.current_rect = None
        self.dragging = False
        self.drag_offset = QPoint()

        self._setup_ui()

    def _setup_ui(self):
        # 无边框窗口
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # 设置窗口大小
        pw = self.pixmap.width()
        ph = self.pixmap.height()
        max_w, max_h = 900, 700
        if pw > max_w or ph > max_h:
            scale = min(max_w / pw, max_h / ph)
            pw = int(pw * scale)
            ph = int(ph * scale)
            self.display_pixmap = self.pixmap.scaled(
                pw, ph, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        else:
            self.display_pixmap = self.pixmap

        toolbar_height = 48
        self.resize(pw, ph + toolbar_height)

        # 主布局
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # 图片画布（无边框）
        self.canvas = QLabel()
        self.canvas.setPixmap(self.display_pixmap)
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setMouseTracking(True)
        self.canvas.mousePressEvent = self._canvas_mouse_press
        self.canvas.mouseMoveEvent = self._canvas_mouse_move
        self.canvas.mouseReleaseEvent = self._canvas_mouse_release
        self.canvas.setStyleSheet("background-color: transparent;")
        self.main_layout.addWidget(self.canvas, 1)

        # 悬浮工具栏（底部居中，黑色圆角）
        self.toolbar = QFrame()
        self.toolbar.setFixedHeight(toolbar_height)
        self.toolbar.setStyleSheet("""
            QFrame {
                background-color: rgb(30, 30, 30);
                border-radius: 24px;
            }
            QPushButton {
                background-color: transparent;
                color: white;
                border: none;
                border-radius: 8px;
                min-width: 36px;
                min-height: 36px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 40);
            }
            QPushButton.active {
                background-color: #007aff;
            }
            .color-btn {
                border-radius: 12px;
                border: 2px solid transparent;
            }
            .color-btn:hover {
                border-color: white;
            }
        """)

        # 工具栏水平布局
        toolbar_layout = QHBoxLayout(self.toolbar)
        toolbar_layout.setContentsMargins(16, 6, 16, 6)
        toolbar_layout.setSpacing(10)

        # 工具按钮
        tools = [
            ("arrow", "↗", "箭头 (A)"),
            ("rectangle", "▢", "矩形 (R)"),
            ("ellipse", "○", "椭圆 (O)"),
            ("text", "T", "文字 (T)"),
            ("pen", "✎", "画笔 (P)"),
            ("highlight", "▤", "高亮 (H)"),
            ("mosaic", "██", "马赛克 (M)"),
        ]

        self.tool_buttons = {}
        for tool_id, icon, tip in tools:
            btn = QPushButton(icon)
            btn.setFixedSize(36, 36)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, t=tool_id: self._select_tool(t))
            self.tool_buttons[tool_id] = btn
            toolbar_layout.addWidget(btn)

        toolbar_layout.addSpacing(10)

        # 颜色选择器
        colors = [
            ("#ff3b30", "红色"),
            ("#007aff", "蓝色"),
            ("#ffcc00", "黄色"),
            ("#34c759", "绿色"),
            ("#000000", "黑色"),
            ("#ffffff", "白色"),
        ]
        self.color_buttons = {}
        for color_hex, name in colors:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color_hex};
                    border-radius: 11px;
                    border: 1px solid rgba(255,255,255,48);
                }}
            """)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            color = QColor(color_hex)
            btn.clicked.connect(lambda checked, c=color: self._select_color(c))
            self.color_buttons[color_hex] = btn
            toolbar_layout.addWidget(btn)

        toolbar_layout.addSpacing(10)

        # 宽度选择
        for w in [2, 4, 6]:
            btn = QPushButton(str(w))
            btn.setFixedSize(28, 28)
            btn.setStyleSheet("""
                QPushButton {
                    color: white;
                    font-weight: bold;
                    font-size: 12px;
                    background-color: transparent;
                }
                QPushButton:hover {
                    background-color: rgba(255,255,255,40);
                    border-radius: 14px;
                }
            """)
            btn.clicked.connect(lambda checked, width=w: self._select_width(width))
            toolbar_layout.addWidget(btn)

        # 弹性空间
        spacer = QLabel()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar_layout.addWidget(spacer)

        # 完成截图按钮 (✓ 勾选图标，自动复制到剪贴板)
        done_btn = QPushButton("✓ 完成")
        done_btn.setFixedSize(80, 36)
        done_btn.setStyleSheet("""
            QPushButton {
                background-color: #34c759;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 14px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2db840;
            }
        """)
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.clicked.connect(self._copy_to_clipboard)
        toolbar_layout.addWidget(done_btn)

        # 保存按钮
        save_btn = QPushButton("💾")
        save_btn.setFixedSize(36, 36)
        save_btn.setToolTip("保存到文件")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: white;
                border: none;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,40);
                border-radius: 18px;
            }
        """)
        save_btn.clicked.connect(self._save_to_file)
        toolbar_layout.addWidget(save_btn)

        # 添加工具栏到主布局（底部居中）
        self.main_layout.addWidget(self.toolbar, 0, Qt.AlignmentFlag.AlignHCenter)

        # 默认选中
        self._select_tool("arrow")
        self._select_color(QColor("#ff3b30"))
        self._select_width(4)

        # 让工具栏可以拖拽整个窗口
        self.toolbar.mousePressEvent = self._toolbar_mouse_press
        self.toolbar.mouseMoveEvent = self._toolbar_mouse_move
        self.toolbar.mouseReleaseEvent = self._toolbar_mouse_release

    def _select_tool(self, tool_id):
        self.current_tool = tool_id
        # 更新按钮样式
        for btn in self.tool_buttons.values():
            btn.setProperty("active", False)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        btn = self.tool_buttons[tool_id]
        btn.setProperty("active", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _select_color(self, color):
        self.current_color = color

    def _select_width(self, width):
        self.current_width = width

    def _toolbar_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _toolbar_mouse_move(self, event):
        if self.dragging:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def _toolbar_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            event.accept()

    def _canvas_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_drawing = True
            self.start_point = event.pos()
            self.current_rect = QRect(self.start_point, self.start_point)

    def _canvas_mouse_move(self, event):
        if self.is_drawing:
            self.current_rect = QRect(self.start_point, event.pos()).normalized()
            self._redraw_canvas()

    def _canvas_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_drawing:
            self.is_drawing = False
            if self.current_rect and self.current_rect.width() > 3 and self.current_rect.height() > 3:
                # 保存标注
                self.annotations.append({
                    "tool": self.current_tool,
                    "rect": QRect(self.current_rect),
                    "color": QColor(self.current_color),
                    "width": self.current_width
                })
            self.current_rect = None
            self._redraw_canvas()

    def _redraw_canvas(self):
        """重新绘制画布"""
        # 创建可修改的pixmap
        self.current_pixmap = self.display_pixmap.copy()
        painter = QPainter(self.current_pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制所有标注
        for ann in self.annotations:
            self._draw_annotation(painter, ann)

        # 绘制当前正在画的标注
        if self.current_rect and self.is_drawing:
            ann = {
                "tool": self.current_tool,
                "rect": self.current_rect,
                "color": self.current_color,
                "width": self.current_width
            }
            self._draw_annotation(painter, ann)

        painter.end()
        self.canvas.setPixmap(self.current_pixmap)

    def _draw_annotation(self, painter, ann):
        """绘制单个标注"""
        tool = ann["tool"]

        if tool == "sequence":
            self._draw_sequence(painter, ann)
            return

        rect = ann["rect"]

        if tool == "rectangle":
            painter.setPen(QPen(ann["color"], ann["width"]))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(rect)
        elif tool == "ellipse":
            painter.setPen(QPen(ann["color"], ann["width"]))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawEllipse(rect)
        elif tool == "arrow":
            # 箭头 - 微信风格（统一多边形）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(ann["color"]))
            self._draw_arrow_head(painter, rect.topLeft(), rect.bottomRight(), ann["width"], ann["color"])
        elif tool == "text":
            font_size = ann.get("width", 12)
            font = QFont("Microsoft YaHei", font_size)
            painter.setFont(font)
            painter.setPen(QPen(ann["color"]))
            text = ann.get("text", "")
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft), text)

    def _draw_sequence(self, painter, ann):
        """绘制序号标注（圆圈 + 连线 + 文字标签）"""
        circle_pos = ann.get("circle_pos")
        radius = ann.get("circle_radius", 12)
        text_pos = ann.get("text_pos")
        text = ann.get("text", "")
        number = ann.get("number", 1)
        color = ann.get("color") or QColor(255, 59, 48)

        if not (circle_pos and text_pos):
            return

        # 连接线
        text_center_y = text_pos.y() + 10
        painter.setPen(QPen(color, 2))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        painter.drawLine(int(circle_pos.x()), int(circle_pos.y()), int(text_pos.x()), int(text_center_y))

        # 实心圆圈
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        rect_circle = QRect(int(circle_pos.x() - radius), int(circle_pos.y() - radius), radius * 2, radius * 2)
        painter.drawEllipse(rect_circle)

        # 圆圈内白色数字
        painter.setPen(QPen(Qt.GlobalColor.white))
        font_num = QFont("Arial", 10, QFont.Weight.Bold)
        painter.setFont(font_num)
        painter.drawText(rect_circle, int(Qt.AlignmentFlag.AlignCenter), str(number))

        # 文字标签
        if text:
            fm = QFontMetrics(QFont("Microsoft YaHei", 12))
            text_width = max(100, fm.horizontalAdvance(text) + 10)
            text_height = 20
            # 有色边框，透明背景
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 1))
            painter.drawRect(int(text_pos.x()), int(text_pos.y()), text_width, text_height)
            # 与圆圈同色文字
            font_text = QFont("Microsoft YaHei", 12)
            painter.setFont(font_text)
            painter.setPen(QPen(color))
            painter.drawText(
                QRect(int(text_pos.x()), int(text_pos.y()), text_width, text_height),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                text
            )

    def _draw_arrow_head(self, painter, start, end):
        """绘制箭头头部"""
        import math
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        arrow_size = 15
        color = painter.pen().color()

        p1 = QPoint(
            int(end.x() - arrow_size * math.cos(angle - math.pi / 6)),
            int(end.y() - arrow_size * math.sin(angle - math.pi / 6))
        )
        p2 = QPoint(
            int(end.x() - arrow_size * math.cos(angle + math.pi / 6)),
            int(end.y() - arrow_size * math.sin(angle + math.pi / 6))
        )

        painter.drawLine(end, p1)
        painter.drawLine(end, p2)

    def _get_result_pixmap(self):
        """获取最终结果图片"""
        if hasattr(self, 'current_pixmap'):
            return self.current_pixmap
        return self.display_pixmap

    def _copy_to_clipboard(self):
        result = self._get_result_pixmap()
        clipboard = QApplication.clipboard()
        clipboard.setPixmap(result)
        self.close()

    def _save_to_file(self):
        from PyQt6.QtWidgets import QFileDialog
        result = self._get_result_pixmap()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "screenshot.png", "PNG Files (*.png)"
        )
        if file_path:
            result.save(file_path, "PNG")
        self.close()


def main():
    """使用 ShortsApp 启动，支持托盘和快捷键"""
    import traceback

    _install_excepthook()
    try:
        shorts_app = ShortsApp()
        return shorts_app.run()
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()