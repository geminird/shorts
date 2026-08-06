"""标注窗口 - 显示截图并添加标注"""
import math
from PyQt6.QtCore import Qt, QRect, QPoint, QPointF, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPen, QBrush, QColor, QFont, QImage, QCursor, QGuiApplication, QPainterPath
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QToolBar, QToolButton

from app.ui.toolbar import AnnotationToolbar
from app.widgets.annotation_item import AnnotationItem


class AnnotationWindow(QWidget):
    """标注编辑窗口"""

    save_requested = pyqtSignal(QPixmap)  # 保存信号
    copy_requested = pyqtSignal(QPixmap)  # 复制信号
    close_requested = pyqtSignal()  # 关闭信号

    def __init__(self, pixmap, region_rect):
        super().__init__()
        self.original_pixmap = pixmap
        self.region_rect = region_rect
        self.annotations = []  # 标注列表
        self.current_tool = "arrow"  # 当前工具
        self.current_color = QColor(255, 59, 48)  # 默认红色
        self.current_width = 4
        self.is_drawing = False
        self.current_annotation = None

        self._setup_ui()

    def _setup_ui(self):
        """设置UI"""
        # 窗口属性
        self.setWindowTitle("Shorts - 标注")
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint
        )

        # 设置窗口大小和位置
        pw = self.original_pixmap.width()
        ph = self.original_pixmap.height()
        toolbar_height = 50

        # 限制最大尺寸
        max_w, max_h = 800, 600
        if pw > max_w or ph > max_h:
            scale = min(max_w / pw, max_h / ph)
            pw = int(pw * scale)
            ph = int(ph * scale)
            self.display_pixmap = self.original_pixmap.scaled(
                pw, ph, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        else:
            self.display_pixmap = self.original_pixmap

        self.resize(pw, ph + toolbar_height)
        self.move(100, 100)

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 截图显示区域
        self.canvas = AnnotationCanvas(self.display_pixmap, self)
        self.canvas.setMinimumSize(pw, ph)
        self.canvas.annotation_added.connect(self._on_annotation_added)
        layout.addWidget(self.canvas, 1)

        # 工具栏
        self.toolbar = AnnotationToolbar(self)
        self.toolbar.tool_changed.connect(self._on_tool_changed)
        self.toolbar.color_changed.connect(self._on_color_changed)
        self.toolbar.width_changed.connect(self._on_width_changed)
        self.toolbar.copy_clicked.connect(self._on_copy)
        self.toolbar.save_clicked.connect(self._on_save)
        self.toolbar.close_clicked.connect(self._on_close)
        layout.addWidget(self.toolbar)

        self.setLayout(layout)

    def _on_tool_changed(self, tool):
        """工具切换"""
        self.current_tool = tool
        self.canvas.set_tool(tool, self.current_color, self.current_width)

    def _on_color_changed(self, color):
        """颜色切换"""
        self.current_color = color
        self.canvas.set_color(color)

    def _on_width_changed(self, width):
        """线条宽度切换"""
        self.current_width = width
        self.canvas.set_width(width)

    def _on_annotation_added(self, annotation):
        """标注添加"""
        self.annotations.append(annotation)

    def _on_copy(self):
        """复制到剪贴板"""
        result = self._render_annotations()
        clipboard = QGuiApplication.clipboard()
        clipboard.setPixmap(result)
        self.close_requested.emit()
        self.close()

    def _on_save(self):
        """保存文件"""
        result = self._render_annotations()
        self.save_requested.emit(result)

    def _on_close(self):
        """关闭"""
        self.close_requested.emit()
        self.close()

    def _render_annotations(self):
        """渲染所有标注到图片"""
        result = self.display_pixmap.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        for ann in self.annotations + ([self.current_annotation] if self.current_annotation else []):
            if ann:
                ann.render(painter)

        painter.end()
        return result


class AnnotationCanvas(QWidget):
    """标注画布"""
    annotation_added = pyqtSignal(object)

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.tool = "arrow"
        self.color = QColor(255, 59, 48)
        self.width = 4
        self.start_point = None
        self.current_rect = None

    def set_tool(self, tool, color, width):
        """设置工具"""
        self.tool = tool
        self.color = color
        self.width = width

    def set_color(self, color):
        """设置颜色"""
        self.color = color

    def set_width(self, width):
        """设置宽度"""
        self.width = width

    def paintEvent(self, event):
        """绘制"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制截图
        painter.drawPixmap(0, 0, self.pixmap)

        # 绘制当前标注
        if self.current_rect and self.start_point:
            painter.setPen(QPen(self.color, self.width))
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

            if self.tool == "rectangle":
                painter.drawRect(self.current_rect)
            elif self.tool == "ellipse":
                painter.drawEllipse(self.current_rect)
            elif self.tool == "arrow":
                self._draw_arrow(painter, self.start_point, self.current_rect.topRight())

    def _draw_arrow(self, painter, start, end):
        """绘制微信风格箭头（带内凹设计）"""
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

        # 根据线宽计算箭头尺寸
        s = max(self.current_width, 1.0)
        head_len = min(length * 0.35, 14 * s)   # 箭头头部长度
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
        painter.setBrush(QBrush(self.current_color))

        path = QPainterPath()
        path.moveTo(points[0])
        for p in points[1:]:
            path.lineTo(p)
        path.closeSubpath()

        painter.drawPath(path)

    def mousePressEvent(self, event):
        """鼠标按下"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_point = event.pos()
            self.current_rect = QRect(self.start_point, self.start_point)

    def mouseMoveEvent(self, event):
        """鼠标移动"""
        if self.start_point:
            self.current_rect = QRect(self.start_point, event.pos()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        """鼠标释放"""
        if event.button() == Qt.MouseButton.LeftButton and self.start_point:
            # 创建标注对象
            ann = AnnotationItem(
                self.tool, self.start_point, event.pos(),
                self.color, self.width
            )
            self.annotation_added.emit(ann)

            self.start_point = None
            self.current_rect = None
            self.update()