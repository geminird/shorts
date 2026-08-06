"""窗口枚举（跨平台）：获取光标下的应用窗口矩形。

对外接口：
    get_window_under_cursor() -> dict | None
        返回 {"rect": QRect(逻辑点), "title": str, "owner": str} 或 None。
        rect 用 Qt 逻辑点坐标系，与 SelectionWindow 的选区坐标系一致。

平台实现：
- Windows：user32.WindowFromPoint + GetWindowRect + GetWindowText（从原
  core/screenshot.py 迁移，零行为改动）。
- macOS：Quartz.CGWindowListCopyWindowInfo 按 z-order 遍历，取光标点命中的
  最顶层普通窗口（kCGWindowLayer==0）。坐标为逻辑点、左上原点，与 Qt 一致。

性能：macOS 枚举全窗口列表有开销，调用方应限频（如 mouseMoveEvent 里节流）。
"""
from PyQt6.QtCore import QRect
from PyQt6.QtGui import QCursor

from utils.platform import is_macos, is_windows


def get_window_under_cursor():
    """返回光标下的窗口信息 dict 或 None。坐标为逻辑点。"""
    if is_windows():
        return _win_get_window_under_cursor()
    if is_macos():
        return _mac_get_window_under_cursor()
    return None


def get_cursor_position():
    """光标位置（逻辑点），跨平台。"""
    p = QCursor.pos()
    return p.x(), p.y()


# ----------------------------- Windows -----------------------------

if is_windows():
    import ctypes
    from ctypes import wintypes

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    def _win_get_window_under_cursor():
        user32 = ctypes.windll.user32
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        hwnd = user32.WindowFromPoint(point)
        if not hwnd:
            return None
        # 向上取顶层窗口（WindowFromPoint 可能返回子窗口/控件）
        root = user32.GetAncestor(hwnd, 2)  # GA_ROOT = 2
        if root:
            hwnd = root
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        # 标题
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        owner = ""
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        return {
            "rect": QRect(
                int(rect.left), int(rect.top),
                int(rect.right - rect.left), int(rect.bottom - rect.top),
            ),
            "title": title,
            "owner": owner,
            "hwnd": hwnd,
        }


# ----------------------------- macOS -----------------------------

if is_macos():
    def _mac_get_window_under_cursor():
        try:
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                CGEventCreate,
            )
        except Exception as e:
            print(f"无法加载 Quartz 窗口枚举：{e}")
            return None

        # 光标位置（全局逻辑点）
        try:
            from Quartz import CGEventGetLocation
            loc = CGEventGetLocation(CGEventCreate(None))
            cx, cy = loc.x, loc.y
        except Exception:
            cx, cy = get_cursor_position()

        # 仅枚举屏幕上的窗口，数组按 z-order 从前到后
        try:
            win_list = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly, kCGNullWindowID
            )
        except Exception:
            return None
        if not win_list:
            return None

        import os
        self_pid = os.getpid()

        for win in win_list:
            try:
                layer = win.get("kCGWindowLayer", 1)
                if layer != 0:
                    continue  # 跳过菜单/状态栏等非普通窗口
                owner_pid = win.get("kCGWindowOwnerPID", 0)
                if owner_pid == self_pid:
                    continue  # 跳过自身
                bounds = win.get("kCGWindowBounds")
                if not bounds:
                    continue
                # bounds 是 dict {Height, Width, X, Y}（逻辑点）
                x = bounds.get("X", 0)
                y = bounds.get("Y", 0)
                w = bounds.get("Width", 0)
                h = bounds.get("Height", 0)
                if w <= 0 or h <= 0:
                    continue
                # 命中测试（CGRect 含点）
                if x <= cx < x + w and y <= cy < y + h:
                    owner = win.get("kCGWindowOwnerName", "")
                    title = win.get("kCGWindowName", "") or ""
                    # 过滤桌面/Dock 等系统元素
                    if owner in ("WindowManager", "Dock", "Control Center",
                                 "SystemUIServer"):
                        continue
                    return {
                        "rect": QRect(int(x), int(y), int(w), int(h)),
                        "title": title,
                        "owner": owner,
                    }
            except Exception:
                continue
        return None
