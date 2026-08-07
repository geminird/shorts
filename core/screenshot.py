"""截图核心模块（跨平台）。

macOS：设 mss.darwin.IMAGE_OPTIONS = 0，使 grab 返回 Retina 2x 物理像素图
（清晰）。输入坐标仍用逻辑点（与 QScreen.geometry / CGWindowBounds 一致），
返回的 QPixmap 设 devicePixelRatio=DPR，使 Qt 自动处理逻辑↔物理坐标映射。

坐标体系：
- 所有 capture_* 的输入参数 = 逻辑点（Qt 坐标）
- mss grab 在 IMAGE_OPTIONS=0 下：输入逻辑点，输出物理像素 2x
- 返回的 QPixmap 设了 devicePixelRatio=DPR
- QPixmap.copy(QRect) 会自动按 DPR 换算（Qt 6 行为：逻辑坐标 → 物理裁剪）
- QPixmap.size() 返回逻辑尺寸（物理/DPR）

Windows：DPR 通常 1.0，行为不变。
"""
import mss
from PIL import Image
from PyQt6.QtGui import QImage, QPixmap

from utils.platform import is_macos, device_pixel_ratio


# macOS：设 IMAGE_OPTIONS=0 强制 Retina 2x 物理像素（清晰）
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
        """获取所有显示器信息（逻辑点坐标）。"""
        with mss.mss() as sct:
            return sct.monitors

    def capture_full_screen(self, monitor_index=1):
        """截取整个屏幕。

        monitor_index=0 = mss 的"所有显示器合并"虚拟屏。
        返回的 QPixmap 设了 devicePixelRatio，drawPixmap/copy 按 Qt 逻辑坐标操作。
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
        """将 mss grab 转换为 QPixmap，设 devicePixelRatio。"""
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img = img.convert("RGBA")

        data = img.tobytes("raw", "RGBA")
        qimage = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimage)
        # 设 devicePixelRatio：让 Qt 知道物理像素/逻辑点的比值。
        # 这样 QPixmap.copy(逻辑 QRect) 自动按 DPR 换算到物理坐标裁剪，
        # drawPixmap 自动缩放到逻辑尺寸显示。
        if self._dpr != 1.0:
            pixmap.setDevicePixelRatio(self._dpr)
        return pixmap
