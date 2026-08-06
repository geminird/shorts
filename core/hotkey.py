"""全局热键管理器（跨平台）。

对外暴露统一接口：
    manager = HotkeyManager(callback)   # callback 无参，触发截图
    manager.start()
    manager.stop()

平台实现：
- Windows：沿用系统 RegisterHotKey + 隐藏窗口接收 WM_HOTKEY。RegisterHotKey 会
  在按键到达任何应用前拦截组合键，因此组合键里的字符（如 'A'）不会泄漏到截图前
  处于焦点的文本框。这是刻意保留的行为，不在 Windows 改用 pynput。
- macOS：用 pynput.keyboard.GlobalHotKeys。pynput 在 macOS 上默认是只读监听、
  不吞键（kCGEventTapOptionListenOnly），因此组合键字符可能到达焦点应用——这是
  已知取舍。回调在 pynput 后台线程触发，禁止直接操作 QWidget，故统一用
  QTimer.singleShot(0, callback) 回到 GUI 主线程。

macOS 首启需用户在「系统设置 → 隐私与安全性 → 辅助功能」授权本进程，否则
监听不生效。
"""
import sys
from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import QWidget

from utils.platform import is_macos, is_windows


class HotkeyManager(QObject):
    """全局热键管理器。内部按平台委托给具体实现。"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self._impl = _create_impl(callback)

    def register(self, key_modifiers=None, key_code=None, callback=None):
        """保留旧接口兼容；真正注册在 start() 里完成（那时才有窗口句柄/事件循环）。"""
        if callback is not None:
            self.callback = callback
            self._impl.callback = callback
        return True

    def start(self):
        self._impl.start()

    def stop(self):
        self._impl.stop()


def _create_impl(callback):
    if is_windows():
        return _WindowsHotkey(callback)
    if is_macos():
        return _MacHotkey(callback)
    # 其他平台（Linux 等）暂未实现，退化为不注册——应用仍可从托盘菜单触发。
    return _NullHotkey(callback)


def _fire_on_gui_thread(callback):
    """把回调投递到 GUI 主线程，避免在监听线程里直接弹窗/操作 QWidget。"""
    if callback is not None:
        QTimer.singleShot(0, callback)


class _NullHotkey:
    def __init__(self, callback):
        self.callback = callback

    def start(self):
        pass

    def stop(self):
        pass


if is_windows():
    import ctypes

    class _HotkeyReceiver(QWidget):
        """隐藏窗口：接收系统热键的 WM_HOTKEY 消息。

        RegisterHotKey 会把热键消息投递给注册时指定的窗口，Qt 主事件循环
        会把该窗口的消息派发给它的 nativeEvent。窗口无需显示。
        """

        def __init__(self):
            super().__init__()
            # 隐藏：无边框、不出现在任务栏
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            )
            self.resize(0, 0)
            self.on_hotkey = None  # WM_HOTKEY 触发回调

        def nativeEvent(self, eventType, message):
            # Windows 上 eventType 为字节串 b"windows_generic_MSG"
            if eventType == b"windows_generic_MSG":
                try:
                    from ctypes import wintypes
                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == 0x0312:  # WM_HOTKEY
                        if self.on_hotkey:
                            self.on_hotkey()
                        return True, 0
                except Exception:
                    pass
            # 不调用 super().nativeEvent()：在 PyQt6 + Python 3.14 下，转发 super
            # 的返回值会触发 native 崩溃。返回 (handled=False, 0) 让 Qt 走默认
            # 窗口过程，正常处理创建/绘制等其余消息。
            return False, 0

    class _WindowsHotkey:
        """Windows RegisterHotKey 实现（吞键，从 main.py 搬迁，零行为改动）。"""

        # Windows 修饰键标志
        MOD_ALT = 0x0001
        MOD_CONTROL = 0x0002
        MOD_SHIFT = 0x0004
        MOD_NOREPEAT = 0x4000  # 按住不重复触发（Win7+）

        # 默认热键：Ctrl + Alt + A（含 MOD_NOREPEAT）
        DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        DEFAULT_VK = 0x41  # VK_A

        def __init__(self, callback):
            self.callback = callback
            self._receiver = None        # 接收 WM_HOTKEY 的隐藏窗口
            self._hotkey_id = 1
            self._registered = False
            self._user32 = None

        def start(self):
            """创建隐藏窗口并向系统注册全局热键。"""
            from ctypes import wintypes

            # 用独立实例 + use_last_error，使错误码可靠
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            # 显式声明参数类型，避免 64 位下 HWND 被截断
            self._user32.RegisterHotKey.argtypes = [
                wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT
            ]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.UnregisterHotKey.restype = wintypes.BOOL

            # 创建隐藏窗口；winId() 会强制创建原生窗口从而拿到 HWND
            self._receiver = _HotkeyReceiver()
            self._receiver.on_hotkey = self._fire
            hwnd = int(self._receiver.winId())

            ok = self._user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                self._hotkey_id,
                self.DEFAULT_MODIFIERS,
                self.DEFAULT_VK,
            )
            if ok:
                self._registered = True
                print("全局快捷键 Ctrl+Alt+A 已注册 (RegisterHotKey)")
            else:
                err = ctypes.get_last_error()
                # 1409 = ERROR_HOTKEY_ALREADY_REGISTERED（被其它程序占用）
                hint = "（该热键可能已被其它程序占用）" if err == 1409 else ""
                print(f"RegisterHotKey 失败，错误码 {err}{hint}")

        def _fire(self):
            _fire_on_gui_thread(self.callback)

        def stop(self):
            if not self._registered or self._receiver is None:
                return
            from ctypes import wintypes
            try:
                hwnd = int(self._receiver.winId())
                self._user32.UnregisterHotKey(wintypes.HWND(hwnd), self._hotkey_id)
            except Exception:
                pass
            self._registered = False


if is_macos():
    # macOS 全局热键：双击 Cmd 键触发，基于 NSEvent FlagsChanged。
    #
    # 选型背景（经大量实测）：
    # - pynput/CGEventTap/Carbon 在 PyQt6 事件循环下都收不到修饰键或会吞字母键；
    # - NSEvent global monitor 能稳定收到 FlagsChanged（修饰键按下/松开），但
    #   收不到字母键的 KeyDown——后者需要「输入监控」权限，而 adhoc 签名的
    #   .app 该权限不稳定。
    # - 因此用"双击右 Cmd 键"作为热键：只依赖 FlagsChanged（仅需辅助功能权限，
    #   已授），绕开字母 KeyDown 限制。iTerm/VS Code 也用此方式唤起，用户熟悉。
    #   判定：Cmd 从松开→按下，且距上次 Cmd 按下 < 350ms，且期间无其他修饰键。
    import time
    from AppKit import (
        NSEvent, NSEventMaskFlagsChanged,
        NSEventModifierFlagCommand,
    )

    _MAC_HOTKEY_MASK = NSEventMaskFlagsChanged
    # Cmd 键键码：左 Cmd=55，右 Cmd=54
    _CMD_KEYCODES = (55, 54)

    class _MacHotkey:
        """macOS 热键：双击 Cmd 键（NSEvent FlagsChanged，需辅助功能权限）。"""

        def __init__(self, callback):
            self.callback = callback
            self._global = None
            self._local = None
            self._last_cmd_down_ts = 0.0  # 上次 Cmd 按下的时间戳

        def _handle(self, event):
            try:
                kc = event.keyCode()
                if kc not in _CMD_KEYCODES:
                    # 期间按了别的键 → 重置双击窗口
                    self._last_cmd_down_ts = 0.0
                    return
                flags = event.modifierFlags()
                now_pressed = bool(flags & NSEventModifierFlagCommand)
                if now_pressed:
                    now = time.monotonic()
                    gap = now - self._last_cmd_down_ts
                    if 0.0 < self._last_cmd_down_ts > 0.0 and gap < 0.35:
                        # 双击 Cmd 触发
                        self._last_cmd_down_ts = 0.0  # 防三连击
                        _fire_on_gui_thread(self.callback)
                    else:
                        self._last_cmd_down_ts = now
                # Cmd 松开时不重置时间戳（双击窗口跨松开→按下）
            except Exception:
                pass

        def _handle_local(self, event):
            self._handle(event)
            return event

        def start(self):
            try:
                self._global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    _MAC_HOTKEY_MASK, self._handle)
                self._local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    _MAC_HOTKEY_MASK, self._handle_local)
                print("热键已注册：双击 Cmd 键触发截图（需辅助功能权限）")
            except Exception as e:
                import traceback
                print(f"热键启动失败：{e}")
                traceback.print_exc()

        def stop(self):
            for attr in ("_global", "_local"):
                mon = getattr(self, attr, None)
                if mon is not None:
                    try:
                        NSEvent.removeMonitor_(mon)
                    except Exception:
                        pass
                    setattr(self, attr, None)

