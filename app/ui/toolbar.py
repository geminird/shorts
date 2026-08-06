"""标注工具栏"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel,
    QFrame, QSizePolicy
)


class AnnotationToolbar(QWidget):
    """标注工具栏"""

    tool_changed = pyqtSignal(str)
    color_changed = pyqtSignal(QColor)
    width_changed = pyqtSignal(int)
    copy_clicked = pyqtSignal()
    save_clicked = pyqtSignal()
    close_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        """设置UI"""
        self.setFixedHeight(50)
        self.setStyleSheet("""
            QWidget {
                background-color: #f5f5f5;
                border-top: 1px solid #ddd;
            }
            QPushButton {
                background-color: white;
                border: 1px solid #ddd;
                border-radius: 4px;
                padding: 6px 12px;
                min-width: 32px;
                min-height: 32px;
            }
            QPushButton:hover {
                background-color: #e8e8e8;
            }
            QPushButton:pressed {
                background-color: #d8d8d8;
            }
            QPushButton.active {
                background-color: #007aff;
                color: white;
                border-color: #0066dd;
            }
            .color-btn {
                border-radius: 4px;
                border: 2px solid transparent;
            }
            .color-btn.selected {
                border-color: #007aff;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        # 关闭按钮
        close_btn = QPushButton("×")
        close_btn.setFixedSize(32, 32)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 20px;
                color: #666;
            }
            QPushButton:hover {
                background-color: #ff3b30;
                color: white;
            }
        """)
        close_btn.clicked.connect(self.close_clicked)
        layout.addWidget(close_btn)

        # 分割线
        layout.addWidget(self._create_separator())

        # 工具按钮
        tools = [
            ("arrow", "→", "箭头 (A)"),
            ("rectangle", "□", "矩形 (R)"),
            ("ellipse", "○", "椭圆 (O)"),
            ("text", "T", "文字 (T)"),
            ("pen", "✎", "画笔 (P)"),
            ("highlight", "▤", "高亮 (H)"),
            ("mosaic", "██", "马赛克 (M)"),
        ]

        self.tool_buttons = {}
        for tool_id, icon, tip in tools:
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn.setFixedSize(36, 36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, t=tool_id: self._select_tool(t))
            self.tool_buttons[tool_id] = btn
            layout.addWidget(btn)

        # 选中的工具
        self._select_tool("arrow")

        # 分割线
        layout.addWidget(self._create_separator())

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
            btn.setFixedSize(24, 24)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color_hex};
                    border: 2px solid #ddd;
                    border-radius: 4px;
                }}
                QPushButton:hover {{
                    border-color: #007aff;
                }}
            """)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            color = QColor(color_hex)
            btn.clicked.connect(lambda checked, c=color: self._select_color(c))
            self.color_buttons[color_hex] = btn
            layout.addWidget(btn)

        # 默认选中红色
        self._select_color(QColor("#ff3b30"))

        # 分割线
        layout.addWidget(self._create_separator())

        # 线条粗细
        widths = [2, 4, 6]
        self.width_buttons = {}
        for w in widths:
            btn = QPushButton(str(w))
            btn.setFixedSize(32, 32)
            btn.setToolTip(f"{w}px")
            btn.clicked.connect(lambda checked, width=w: self._select_width(width))
            self.width_buttons[w] = btn
            layout.addWidget(btn)

        self._select_width(4)

        # 分割线
        layout.addWidget(self._create_separator())

        # 复制按钮
        copy_btn = QPushButton("复制")
        copy_btn.setFixedSize(60, 36)
        copy_btn.clicked.connect(self.copy_clicked)
        layout.addWidget(copy_btn)

        # 保存按钮
        save_btn = QPushButton("保存")
        save_btn.setFixedSize(60, 36)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #007aff;
                color: white;
                border: none;
            }
            QPushButton:hover {
                background-color: #0066dd;
            }
        """)
        save_btn.clicked.connect(self.save_clicked)
        layout.addWidget(save_btn)

        # 弹性空间
        spacer = QLabel()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(spacer)

    def _create_separator(self):
        """创建分割线"""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #ddd;")
        return sep

    def _select_tool(self, tool_id):
        """选择工具"""
        # 取消之前的选中状态
        for btn in self.tool_buttons.values():
            btn.setProperty("active", False)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        # 设置新的选中状态
        btn = self.tool_buttons[tool_id]
        btn.setProperty("active", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

        self.tool_changed.emit(tool_id)

    def _select_color(self, color):
        """选择颜色"""
        self.color_changed.emit(color)

    def _select_width(self, width):
        """选择线条粗细"""
        # 取消之前的选中状态
        for btn in self.width_buttons.values():
            btn.setProperty("active", False)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        # 设置新的选中状态
        btn = self.width_buttons[width]
        btn.setProperty("active", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

        self.width_changed.emit(width)

    def _connect_signals(self):
        """连接信号"""
        pass