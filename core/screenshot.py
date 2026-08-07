"""截图核心模块（跨平台）。

macOS：设 mss.darwin.IMAGE_OPTIONS = 0，使 grab 返回 Retina 2x 物理像素图
（清晰）。返回的 QPixmap 设 devicePixelRatio=DPR，使 drawPixmap 自动清晰显示，
copy/toImage 按物理像素操作。

所有 capture_* 的输入参数 = 逻辑点（Qt 坐标），和之前一样不变。
"""
import mss
from PIL import Image
from PyQt6.QtGui import QImage, QPixmap

from utils.platform import is_macos, device_pixel_ratio


if is_macos():
    try:
        from mss import darwin as _mss_darwin
        _mss_darwin.IMAGE_OPTIONS = 0
    except Exception:
        pass


class Screenshot:
    """截图管理器"""

    def __init__(self):
        self._dpr = device_pixel_ratio()

    def get_monitors(self):
        with mss.mss() as sct:
            return sct.monitors

    def capture_full_screen(self, monitor_index=1):
        with mss.mss() as sct:
            monitor = sct.monitors[monitor_index]
            screenshot = sct.grab(monitor)
            return self._grab_to_pixmap(screenshot)

    def capture_region(self, x, y, width, height):
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
        """将 mss grab 转换为 QPixmap，设 devicePixelRatio=DPR。"""
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img = img.convert("RGBA")
        data = img.tobytes("raw", "RGBA")
        qimage = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimage)
        if self._dpr != 1.0:
            pixmap.setDevicePixelRatio(self._dpr)
        return pixmap
