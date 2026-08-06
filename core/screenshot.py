"""截图核心模块（跨平台）。

基于 mss 抓屏。所有坐标统一用 Qt 逻辑点（与 QScreen.geometry() / CGWindowBounds
一致）。返回的 QPixmap **不设 devicePixelRatio**（保持 mss 默认的 1:1，逻辑像素图），
这样 QPixmap.copy(QRect) 按逻辑坐标裁剪即为正确区域，不会因 DPR 换算偏移。

- macOS：使用 mss 默认行为（返回逻辑像素 1x 图）。坐标原点在主屏左上，与
  CGWindowList 一致。清晰度虽为 1x（文字略软），但坐标/裁剪完全正确——这是
  跨平台最稳的选择。若后续要 Retina 清晰度，应在导出时按 DPR 上采样，而非在
  截取阶段引入 IMAGE_OPTIONS=0（会让 mss 的 monitors/grab 坐标系错乱）。
- Windows：行为与改造前一致。
"""
import mss
from PIL import Image
from PyQt6.QtGui import QImage, QPixmap

from utils.platform import is_macos


class Screenshot:
    """截图管理器"""

    def __init__(self):
        pass

    def get_monitors(self):
        """获取所有显示器信息（逻辑点坐标）。"""
        with mss.mss() as sct:
            return sct.monitors

    def capture_full_screen(self, monitor_index=1):
        """截取整个屏幕。

        monitor_index=0 = mss 的“所有显示器合并”虚拟屏。
        返回逻辑像素 QPixmap（devicePixelRatio=1，按逻辑坐标裁剪即可）。
        """
        with mss.mss() as sct:
            monitor = sct.monitors[monitor_index]
            screenshot = sct.grab(monitor)
            return self._grab_to_pixmap(screenshot)

    def capture_region(self, x, y, width, height):
        """截取指定区域（坐标为 Qt 逻辑点）。"""
        monitor = {
            "left": round(x),
            "top": round(y),
            "width": round(width),
            "height": round(height),
        }
        with mss.mss() as sct:
            screenshot = sct.grab(monitor)
            return self._grab_to_pixmap(screenshot)

    def _grab_to_pixmap(self, screenshot):
        """将 mss grab 转换为 QPixmap。"""
        # mss 返回的是 BGRA 格式
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img = img.convert("RGBA")

        data = img.tobytes("raw", "RGBA")
        qimage = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
        # 不设 devicePixelRatio：mss 返回逻辑像素图，1:1 对应屏幕逻辑坐标，
        # QPixmap.copy(QRect) 直接按逻辑坐标裁剪即正确。
        return QPixmap.fromImage(qimage)
