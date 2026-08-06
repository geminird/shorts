"""跨平台工具：平台判断与高 DPI 信息。

集中所有 sys.platform 分支与 devicePixelRatio 查询，避免业务代码里散落
ctypes/winreg/Quartz 导入。新增 Windows 专属或 macOS 专属逻辑前先经此模块判断。
"""
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


def device_pixel_ratio() -> float:
    """主屏物理像素 / 逻辑点 的比值。

    Retina 为 2.0；Windows 在系统缩放 100% 时为 1.0，125%/150% 时为对应值。
    mss 在 macOS 上返回物理像素，而 Qt 选区用逻辑点，二者之间的换算用它。
    """
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return 1.0
    return float(screen.devicePixelRatio())


def overlay_window_flags():
    """浮层子窗口（工具栏/样式面板/进度条等）的窗口标志。

    macOS 上光 WindowStaysOnTopHint 不足以盖住 Dock，需叠加 Qt.Tool（工具窗口）
    才能浮于 Dock 之上且不抢焦点。返回 Frameless + StaysOnTop（+ macOS 下 Tool）。
    """
    flags = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
    )
    if is_macos():
        flags |= Qt.WindowType.Tool
    return flags


def activate_foreground_app(except_widget=None):
    """把焦点/激活状态交给"最前面的非自身应用"。

    用于滚动截图：选区窗口隐藏后，Shorts 仍是 frontmost，用户的滚轮/触控板
    滚动送不到被截的目标应用。这里通过 CGWindowList 找到最前面的非自身
    layer=0 窗口的 owner，用 NSRunningApplication 激活它，让滚动直达目标。
    Windows/Linux 上为空操作。
    """
    if not is_macos():
        return
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return
        import os
        from Quartz import (
            CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
        from AppKit import NSRunningApplication
    except Exception:
        return
    me = os.getpid()
    try:
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        for w in wins:
            if w.get("kCGWindowLayer", 1) != 0:
                continue
            pid = w.get("kCGWindowOwnerPID", 0)
            if pid in (0, me):
                continue
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None or app.isFinished() or app.isTerminated():
                continue
            # NSApplicationActivateIgnoringOtherApps = 1 << 3
            app.activateWithOptions_(1 << 3)
            return
    except Exception:
        pass


def make_floating_panel(widget):
    """把 Qt 窗口的底层 NSWindow 转成浮动面板（non-activating NSPanel）。

    macOS 上普通 NSWindow 在 app 失去前台状态时会被其他 app 窗口压下去。
    NSPanel(nonactivatingPanel) 是系统支持的"浮动面板"（像 Spotlight），
    即使其他 app 在前台，它也始终浮在最前，且不抢键盘焦点。
    用法：widget.show() 之后调用。
    """
    if not is_macos():
        return
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return
        import objc
        from AppKit import (
            NSStatusWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
        )
    except Exception:
        return
    try:
        wid = int(widget.winId())
        if not wid:
            return
        view = objc.objc_object(c_void_p=wid)
        ns_win = view.window()
        if ns_win is None:
            return
        # 设成 non-activating panel 行为（不抢焦点，但始终浮在前）
        ns_win.setBecomesKeyOnlyIfNeeded_(True)
        # 最高层级
        ns_win.setLevel_(NSStatusWindowLevel)
        # 所有 Space 可见
        behavior = (
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        ns_win.setCollectionBehavior_(behavior)
        # 关键：setHidesOnDeactivate_ 确保切换 app 时窗口不隐藏
        ns_win.setHidesOnDeactivate_(False)
    except Exception:
        pass


def raise_overlay(widget):
    """把浮层窗口提到所有普通窗口之上（包括 macOS 的 Dock）。

    macOS 上 Qt 的 WindowStaysOnTopHint/Tool 仍会被系统 Dock 压住。这里在
    widget.show() 之后调用，通过 winId 桥接到底层 NSWindow，把 level 提到
    NSStatusWindowLevel（高于 Dock）。Windows/Linux 上为空操作。
    必须在 widget.show() 之后调用（否则还没有 NSWindow）。
    """
    if not is_macos():
        return
    # 必须在真实 Cocoa 平台下才有效（offscreen/headless 无 NSWindow，解引用会崩）
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return
        import objc
        from AppKit import NSStatusWindowLevel
    except Exception:
        return
    try:
        wid = int(widget.winId())
        if not wid:
            return
        view = objc.objc_object(c_void_p=wid)
        ns_win = view.window()
        if ns_win is not None:
            # 设最高层级（高于 Dock 和普通窗口）
            ns_win.setLevel_(NSStatusWindowLevel)
            # collectionBehavior：加入所有 Space + 不参与 Exposé/Spaces 切换
            # 这样切换 app/Space 时窗口始终保持在最前面
            try:
                from AppKit import (
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle,
                )
                behavior = (
                    NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehaviorStationary
                    | NSWindowCollectionBehaviorFullScreenAuxiliary
                    | NSWindowCollectionBehaviorIgnoresCycle
                )
                ns_win.setCollectionBehavior_(behavior)
            except Exception:
                pass
    except Exception:
        pass


def make_mouse_passthrough(widget):
    """让窗口对鼠标事件完全透明（含滚轮），底层窗口能收到点击/滚动。

    macOS 上 Qt 的 WA_TransparentForMouseEvents 对滚轮事件不可靠，尤其是窗口
    处于高层级时。这里用原生 NSWindow.setIgnoresMouseEvents:YES 彻底让鼠标
    穿透——用于滚动截图的全屏遮罩：它需要显示像素但绝不能拦截用户滚轮。
    Windows/Linux 上为空操作（WA_TransparentForMouseEvents 已够用）。
    """
    if not is_macos():
        return
    try:
        from PyQt6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return
        import objc
    except Exception:
        return
    try:
        wid = int(widget.winId())
        if not wid:
            return
        view = objc.objc_object(c_void_p=wid)
        ns_win = view.window()
        if ns_win is not None:
            ns_win.setIgnoresMouseEvents_(True)
    except Exception:
        pass



