# Shorts - 截图标注工具

跨平台截图标注工具（Windows / macOS），仿 Mac iShot 功能。

## 功能

- 区域截图
- 窗口截图（鼠标悬停高亮、单击选中整窗口）
- 全屏截图
- 滚动截图（长截图，自动拼接）
- **GIF 录制**（选区 → 录制 → 生成 GIF）
- **OCR 文字识别**（macOS Vision，中英文）
- 标注工具：箭头、直线（实线/虚线）、矩形、椭圆、文字、画笔、高亮、马赛克（块状/高斯模糊）、序号、取色器
- 标注描边（用户选描边色）
- 复制到剪贴板 / 保存文件
- 开机自启、全局热键

## 安装

```bash
pip install -r requirements.txt
```

> macOS：会额外安装 `pyobjc`（窗口枚举）和 `pynput`（全局热键）。

## 运行

```bash
python main.py
```

## 全局热键

| 平台 | 快捷键 |
|------|--------|
| Windows | Ctrl + Alt + A |
| macOS   | Cmd + Alt + A |

## macOS 首启权限（需手动授权一次）

1. **辅助功能**（全局热键）：系统设置 → 隐私与安全性 → 辅助功能
2. **屏幕录制**（截图）：系统设置 → 隐私与安全性 → 屏幕录制

未授权时热键不触发 / 截图全黑。

## 打包

```bash
pyinstaller shorts_app.spec
```

Windows 产出 `dist/Shorts.exe`；macOS 产出 `dist/Shorts.app`。
