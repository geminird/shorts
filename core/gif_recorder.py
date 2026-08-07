"""GIF 录制：选区 → QTimer 抓帧 → Pillow 拼成 GIF。

用 QTimer 在主线程定时抓帧（不用子线程/信号），避免所有跨线程图像数据问题。
对外（被 SelectionWindow 调用）：
    GifRecorder(rect, show_clicks=True, on_frame=callback) → .start() / .stop()
可选 show_clicks：录制时在鼠标点击位置画涟漪+左/右键标记。
"""
import time

from PyQt6.QtCore import QObject, pyqtSignal, QRect, QPointF, QTimer
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QPen, QFont


_CLICK_TTL = 1.0    # 涟漪持续 1 秒
_CLICK_MAX_R = 50

# 瞬时标注参数
_ANN_TTL = 2.0      # 标注存活 2 秒
_ANN_BLINK_COUNT = 5  # 闪烁 5 次


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
        # 瞬时标注列表：[(timestamp, {tool, rect/start_pos/end_pos, color, width, text})]
        self._annotations = []
        # 当前绘制中的标注状态
        self._ann_tool = None       # "rect" / "arrow" / None
        self._ann_color = (255, 59, 48, 255)  # RGBA
        self._ann_width = 3
        self._ann_start = None      # QPoint
        self._ann_current = None    # QPoint（绘制预览用）
        self._ann_drawing = False

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

    # ---------- 瞬时标注管理 ----------

    def set_tool(self, tool):
        """设置当前标注工具: "rect" / "arrow" / None。"""
        self._ann_tool = tool

    def set_color(self, rgba_tuple):
        self._ann_color = rgba_tuple

    def add_annotation(self, ann_data):
        """添加一个已完成的标注（带时间戳，自动过期消失）。"""
        import time as _t
        ann_data["_ts"] = _t.monotonic()
        self._annotations.append(ann_data)

    def clear_annotations(self):
        self._annotations.clear()

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
        # 叠加标注 + 点击效果
        frame = self._draw_overlays(frame)
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

    def _draw_overlays(self, pixmap):
        """把活跃标注（闪烁）+ 点击涟漪叠加到帧上，返回新 QPixmap。

        标注用 PIL 绘制（和点击涟漪一样），闪烁效果：前 _ANN_TTL 秒内交替
        显示/半透明，到期自动移除。
        """
        import time as _t
        now = _t.monotonic()
        # 清理过期标注
        self._annotations = [a for a in self._annotations if now - a["_ts"] < _ANN_TTL]

        # 收集活跃点击
        active_clicks = [(ts, lx, ly, btn) for (ts, lx, ly, btn) in self._clicks
                         if now - ts < _CLICK_TTL]
        # 活跃标注（带闪烁 alpha）
        active_anns = []
        for a in self._annotations:
            age = now - a["_ts"]
            progress = age / _ANN_TTL
            # 闪烁：交替全显示/半透明
            blink_phase = (age / _ANN_TTL) * _ANN_BLINK_COUNT
            alpha = 255 if int(blink_phase) % 2 == 0 else 0
            if alpha == 0:
                continue
            active_anns.append((a, alpha))

        if not active_clicks and not active_anns and not getattr(self, "_preview_ann", None):
            return pixmap

        from PIL import Image, ImageDraw
        qimg = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        bits = qimg.bits()
        bits.setsize(qimg.sizeInBytes())
        pil = Image.frombytes("RGBA", (qimg.width(), qimg.height()), bytes(bits))
        W, H = pil.size
        DPR = pixmap.devicePixelRatio() or 1.0
        SS = 2
        big = pil.resize((W * SS, H * SS), Image.LANCZOS)
        draw = ImageDraw.Draw(big)

        # 画标注
        for ann, alpha in active_anns:
            self._draw_ann_pil(draw, ann, alpha, SS, DPR)
        # 画预览
        preview = getattr(self, "_preview_ann", None)
        if preview:
            self._draw_ann_pil(draw, preview, 255, SS, DPR)

        # 画点击涟漪
        for ts, lx, ly, btn in active_clicks:
            age = now - ts
            t = age / _CLICK_TTL
            if t > 0.85:
                continue
            eased = 1 - (1 - t) ** 3
            r_main = int((6 + eased * (_CLICK_MAX_R - 6)) * SS)
            r_outer = int((10 + eased * (_CLICK_MAX_R + 8)) * SS)
            cx, cy = int(lx * SS * DPR), int(ly * SS * DPR)
            color = (59, 130, 246) if btn == "left" else (251, 146, 60)
            if r_outer > r_main:
                draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
                             outline=(255, 255, 255), width=max(1, int(1.5 * SS)))
            draw.ellipse([cx - r_main, cy - r_main, cx + r_main, cy + r_main],
                         outline=color, width=max(2, int(4 * SS)))
            r_inner = int(max(0, (14 * (1 - t))) * SS)
            if r_inner > 0 and t < 0.6:
                draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=color)
                hl_r = max(1, r_inner // 3)
                draw.ellipse([cx - hl_r, cy - hl_r, cx + hl_r, cy + hl_r], fill=(255, 255, 255))

        pil = big.resize((W, H), Image.LANCZOS)
        data = pil.tobytes("raw", "RGBA")
        out = QImage(data, W, H, QImage.Format.Format_RGBA8888).copy()
        return QPixmap.fromImage(out)

    def _draw_ann_pil(self, draw, ann, alpha, SS, DPR=1.0):
        """用 PIL 绘制单个标注。坐标是逻辑点，需乘 DPR（物理像素）和 SS（超采样）。"""
        color = ann.get("color", (255, 59, 48, 255))
        r, g, b = color[0], color[1], color[2]
        draw_color = (r, g, b, alpha) if len(color) >= 4 else (r, g, b)
        S = SS * DPR  # 总缩放倍率
        w = max(2, int(ann.get("width", 3) * S))
        tool = ann.get("tool", "")

        if tool == "rect":
            rect = ann.get("rect")
            if rect:
                x0, y0 = int(rect[0] * S), int(rect[1] * S)
                x1, y1 = int(rect[2] * S), int(rect[3] * S)
                draw.rectangle([x0-2, y0-2, x1+2, y1+2], outline=(255,255,255,alpha), width=max(4, w+2))
                draw.rectangle([x0, y0, x1, y1], outline=draw_color, width=max(4, w))
        elif tool == "arrow":
            sp = ann.get("start_pos")
            ep = ann.get("end_pos")
            if sp and ep:
                sx, sy = int(sp[0] * S), int(sp[1] * S)
                ex, ey = int(ep[0] * S), int(ep[1] * S)
                draw.line([sx, sy, ex, ey], fill=draw_color, width=w)
                import math
                angle = math.atan2(ey - sy, ex - sx)
                head_len = max(8, w * 3)
                for da in [-0.5, 0.5]:
                    hx = ex - head_len * math.cos(angle + da)
                    hy = ey - head_len * math.sin(angle + da)
                    draw.line([ex, ey, int(hx), int(hy)], fill=draw_color, width=w)


def frames_from_files_to_gif(file_paths, output_path, fps=12):
    """从磁盘 PNG 文件列表编码 GIF（不在内存存所有帧）。"""
    if not file_paths:
        return False
    from PIL import Image
    duration = max(1, round(1000 / fps))
    first = Image.open(file_paths[0]).convert("RGB")
    rest = [Image.open(f).convert("RGB") for f in file_paths[1:]]
    first.save(output_path, save_all=True, append_images=rest,
               duration=duration, loop=0, disposal=1, optimize=False)
    return True


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
