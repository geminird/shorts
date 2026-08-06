"""标注元素项"""
import math
from PyQt6.QtCore import QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen, QBrush, QColor, QFont


class AnnotationItem:
    """标注元素基类"""

    def __init__(self, tool_type, start_pos, end_pos, color, width):
        self.tool_type = tool_type
        self.start_pos = start_pos if isinstance(start_pos, QPoint) else QPoint(*start_pos) if isinstance(start_pos, (tuple, list)) else start_pos
        self.end_pos = end_pos if isinstance(end_pos, QPoint) else QPoint(*end_pos) if isinstance(end_pos, (tuple, list)) else end_pos
        self.color = color if isinstance(color, QColor) else QColor(color)
        self.width = width

    def render(self, painter):
        """渲染标注"""
        painter.setPen(QPen(self.color, self.width))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))

        if self.tool_type == "rectangle":
            rect = QRect(self.start_pos, self.end_pos).normalized()
            painter.drawRect(rect)

        elif self.tool_type == "ellipse":
            rect = QRect(self.start_pos, self.end_pos).normalized()
            painter.drawEllipse(rect)

        elif self.tool_type == "arrow":
            self._draw_arrow_head(painter)

        elif self.tool_type == "text":
            font = QFont()
            font.setPixelSize(18)
            painter.setFont(font)
            painter.drawText(self.start_pos, "文字")

        elif self.tool_type == "highlight":
            rect = QRect(self.start_pos, self.end_pos).normalized()
            highlight_color = QColor(self.color)
            highlight_color.setAlpha(100)
            painter.setBrush(QBrush(highlight_color))
            painter.setPen(QPen(highlight_color, 1))
            painter.drawRect(rect)

    def _draw_arrow_head(self, painter):
        """绘制微信风格箭头头部（带内凹设计）"""
        dx = self.end_pos.x() - self.start_pos.x()
        dy = self.end_pos.y() - self.start_pos.y()
        if dx == 0 and dy == 0:
            return

        length = math.hypot(dx, dy)
        if length < 10:
            return

        angle = math.atan2(dy, dx)
        sin_a = math.sin(angle)
        cos_a = math.cos(angle)

        # 根据线宽计算箭头尺寸
        s = max(self.width, 1.0)
        head_len = min(length * 0.35, 14 * s)   # 箭头头部长度
        head_hw = head_len * 0.5                 # 翼展半宽
        stem_hw = head_hw * 0.25                 # 箭柄半宽

        # 翼展基线
        base_x = self.end_pos.x() - head_len * cos_a
        base_y = self.end_pos.y() - head_len * sin_a

        # 内凹偏移
        notch_fwd = head_len * 0.3
        notch_x = base_x + notch_fwd * cos_a
        notch_y = base_y + notch_fwd * sin_a

        # 6个顶点（尾部尖点共用）
        points = [
            # 1. 尾部尖点
            QPointF(self.start_pos.x(), self.start_pos.y()),
            # 2. 箭柄右侧 → 内凹点
            QPointF(notch_x + stem_hw * sin_a, notch_y - stem_hw * cos_a),
            # 3. 右翼尖端
            QPointF(base_x + head_hw * sin_a, base_y - head_hw * cos_a),
            # 4. 箭尖
            QPointF(self.end_pos.x(), self.end_pos.y()),
            # 5. 左翼尖端
            QPointF(base_x - head_hw * sin_a, base_y + head_hw * cos_a),
            # 6. 箭柄左侧 → 内凹点
            QPointF(notch_x - stem_hw * sin_a, notch_y + stem_hw * cos_a),
        ]

        # 绘制填充的箭头
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self.color))

        path = QPainterPath()
        path.moveTo(points[0])
        for p in points[1:]:
            path.lineTo(p)
        path.closeSubpath()

        painter.drawPath(path)


class TextAnnotation(AnnotationItem):
    """文字标注"""

    def __init__(self, start_pos, color, font_size=18, text=""):
        super().__init__("text", start_pos, start_pos, color, font_size)
        self.text = text
        self.font_size = font_size

    def render(self, painter):
        font = QFont()
        font.setPixelSize(self.font_size)
        painter.setFont(font)
        painter.setPen(QPen(self.color))
        painter.drawText(self.start_pos, self.text)


class MosaicAnnotation(AnnotationItem):
    """马赛克标注"""

    def __init__(self, rect, block_size=10):
        super().__init__("mosaic", rect.topLeft(), rect.bottomRight(), QColor(0, 0, 0), 1)
        self.rect = rect.normalized()
        self.block_size = block_size

    def render(self, painter):
        # 马赛克效果：绘制小方块
        x = self.rect.left()
        y = self.rect.top()
        w = self.rect.width()
        h = self.rect.height()

        for i in range(0, w, self.block_size):
            for j in range(0, h, self.block_size):
                # 使用灰色
                gray = ((i // self.block_size) % 2 * 50 + (j // self.block_size) % 2 * 50 + 128) % 256
                painter.setPen(QPen(QColor(gray, gray, gray), 1))
                painter.setBrush(QBrush(QColor(gray, gray, gray)))
                bx = min(x + i, self.rect.right())
                by = min(y + j, self.rect.bottom())
                bw = min(self.block_size, self.rect.right() - bx)
                bh = min(self.block_size, self.rect.bottom() - by)
                painter.drawRect(bx, by, bw, bh)