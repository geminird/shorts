"""GIF 录制：选区 → QTimer 抓帧 → Pillow 拼成 GIF。

用 QTimer 在主线程定时抓帧（不用子线程/信号），避免所有跨线程图像数据问题。
对外（被 SelectionWindow 调用）：
    GifRecorder(rect, show_clicks=True, on_frame=callback) → .start() / .stop()
可选 show_clicks：录制时在鼠标点击位置画涟漪+左/右键标记。
"""
import time

from PyQt6.QtCore import QObject, pyqtSignal, QRect, QPointF, QTimer
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QPen, QFont


_CLICK_TTL = 1.0    # 涟漪持续 1 秒（约 12 帧 @12fps）
_CLICK_MAX_R = 50   # 最大半径


class GifRecorder(QObject):
    """QTimer 主线程抓帧录制 GIF。每帧回调 on_frame(QPixmap)。"""

    frame_captured = pyqtSignal(QPixmap)

    def __init__(self, rect, fps=12, show_clicks=True):
        super().__init__()
        self._rect = QRect(rect)
        self._interval = int(1000 / fps)
        self._running = False
        self._show_clicks = show_clicks
        self._timer = None
        self._shot = None
        # 点击记录
        self._clicks = []
        self._prev_buttons = 0

    def start(self):
        from core.screenshot import Screenshot
        self._shot = Screenshot()
        self._running = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self._interval)

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None

    def stop_tap(self):
        pass

    def _tick(self):
        """主线程每帧：抓帧 + 轮询点击 + 绘制点击效果 + 发信号。"""
        if not self._running or not self._rect.isValid():
            return
        frame = self._shot.capture_region(
            self._rect.x(), self._rect.y(),
            self._rect.width(), self._rect.height()
        )
        # 轮询鼠标点击
        self._poll_click()
        # 叠加点击效果
        frame = self._draw_clicks(frame)
        self.frame_captured.emit(frame)

    def _poll_click(self):
        """轮询鼠标按键状态，检测 0→按下 跳变记录点击。"""
        try:
            import sys as _sys
            if _sys.platform != "darwin":
                return
            from Quartz import (
                CGEventSourceButtonState, kCGEventSourceStateHIDSystemState,
                kCGMouseButtonLeft, kCGMouseButtonRight,
            )
            left = bool(CGEventSourceButtonState(kCGEventSourceStateHIDSystemState, kCGMouseButtonLeft))
            right = bool(CGEventSourceButtonState(kCGEventSourceStateHIDSystemState, kCGMouseButtonRight))
            cur = (1 if left else 0) | (2 if right else 0)
        except Exception:
            return
        new_pressed = cur & ~self._prev_buttons
        self._prev_buttons = cur
        if not new_pressed:
            return
        try:
            from Quartz import CGEventCreate, CGEventGetLocation
            loc = CGEventGetLocation(CGEventCreate(None))
            gx, gy = loc.x, loc.y
        except Exception:
            return
        lx = gx - self._rect.x()
        ly = gy - self._rect.y()
        if not (0 <= lx <= self._rect.width() and 0 <= ly <= self._rect.height()):
            return
        now = time.monotonic()
        if new_pressed & 1:
            self._clicks.append((now, lx, ly, "left"))
        if new_pressed & 2:
            self._clicks.append((now, lx, ly, "right"))

    def _draw_clicks(self, pixmap):
        """把活跃点击效果叠加到帧上，返回新 QPixmap。

        Material Design 风格涟漪：多层圆环 ease-out 扩散 + 中心光点。
        用 PIL 在 2x 超采样图上画（实现抗锯齿），再缩回原始尺寸。
        """
        now = time.monotonic()
        active = [(ts, lx, ly, btn) for (ts, lx, ly, btn) in self._clicks
                  if now - ts < _CLICK_TTL]
        if not active:
            return pixmap

        from PIL import Image, ImageDraw
        # QPixmap → PIL Image (RGBA)
        qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        bits = qimg.bits()
        bits.setsize(qimg.sizeInBytes())
        pil = Image.frombytes("RGBA", (qimg.width(), qimg.height()), bytes(bits))
        W, H = pil.size

        # 2x 超采样画布（实现抗锯齿），画完缩回
        SS = 2
        big = pil.resize((W * SS, H * SS), Image.LANCZOS)
        draw = ImageDraw.Draw(big)

        for ts, lx, ly, btn in active:
            age = now - ts
            t = age / _CLICK_TTL  # 线性进度 0→1
            if t > 0.85:
                continue
            # ease-out cubic（开始快扩散，后面减速）
            eased = 1 - (1 - t) ** 3
            r_main = int((6 + eased * (_CLICK_MAX_R - 6)) * SS)
            r_outer = int((10 + eased * (_CLICK_MAX_R + 8)) * SS)
            r_inner = int(max(0, (14 * (1 - t))) * SS)  # 中心光点：大→缩→消失
            cx, cy = int(lx * SS), int(ly * SS)

            # 配色：左键=亮蓝，右键=暖橙
            if btn == "left":
                main_rgb = (59, 130, 246)    # 蓝
            else:
                main_rgb = (251, 146, 60)    # 橙

            # 第一层：外光晕环（白色，细，快扩散）
            if r_outer > r_main:
                bbox_o = [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer]
                draw.ellipse(bbox_o, outline=(255, 255, 255), width=max(1, int(1.5 * SS)))

            # 第二层：主彩色环（粗，ease-out 扩散）
            bbox_m = [cx - r_main, cy - r_main, cx + r_main, cy + r_main]
            draw.ellipse(bbox_m, outline=main_rgb, width=max(2, int(4 * SS)))

            # 第三层：中心光点（大→缩→消失，前 60% 生命周期）
            if r_inner > 0 and t < 0.6:
                bbox_i = [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner]
                draw.ellipse(bbox_i, fill=main_rgb)
                # 中心白色高光
                hl_r = max(1, r_inner // 3)
                bbox_h = [cx - hl_r, cy - hl_r, cx + hl_r, cy + hl_r]
                draw.ellipse(bbox_h, fill=(255, 255, 255))

        # 缩回原始尺寸（LANCZOS 缩放 = 抗锯齿效果）
        pil = big.resize((W, H), Image.LANCZOS)

        # PIL → QPixmap
        data = pil.tobytes("raw", "RGBA")
        out = QImage(data, W, H, QImage.Format.Format_RGBA8888).copy()
        return QPixmap.fromImage(out)


def frames_to_gif(frames, path, fps=12):
    """把 QPixmap 列表存成 GIF。"""
    if not frames:
        return False
    from PIL import Image
    duration = max(1, round(1000 / fps))
    pil_frames = []
    for pm in frames:
        img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        bits = img.bits()
        bits.setsize(img.sizeInBytes())
        pil = Image.frombytes("RGBA", (img.width(), img.height()), bytes(bits))
        pil_frames.append(pil.convert("RGB"))
    if not pil_frames:
        return False
    first = pil_frames[0]
    rest = pil_frames[1:]
    first.save(
        path,
        save_all=True,
        append_images=rest,
        duration=duration,
        loop=0,
        disposal=1,  # 不清帧（保留前一帧），让点击效果跨帧持续可见
        optimize=False,  # optimize 会合并相同帧，可能丢掉只有点击效果差异的帧
    )
    return True
