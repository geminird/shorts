"""开机自启（跨平台）。

对外统一接口：
    is_enabled() -> bool
    enable()
    disable()

平台实现：
- Windows：读写 HKCU\\...\\Run 注册表（沿用原 main.py 逻辑，零行为改动）。
- macOS：写 ~/Library/LaunchAgents/com.shorts.app.plist，RunAtLoad 触发。
  开发环境指向 python + main.py；打包后若在 .app bundle 内则指向可执行文件。

托盘菜单「开机自启」的勾选状态由 is_enabled() 驱动。
"""
import sys
from pathlib import Path

from utils.platform import is_macos, is_windows

APP_NAME = "Shorts"
MAC_BUNDLE_ID = "com.shorts.app"


def _start_command():
    """构造启动命令：打包环境用自身可执行文件，开发环境用 python + main.py。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 产物：直接运行可执行文件/.app
        return [sys.executable]
    # 开发环境：pythonw (Windows 无控制台) 或 python3
    python = "pythonw.exe" if is_windows() else sys.executable
    script = str(Path(__file__).resolve().parent.parent / "main.py")
    return [python, script]


def is_enabled() -> bool:
    if is_windows():
        return _win_is_enabled()
    if is_macos():
        return _mac_plist_path().exists()
    return False


def enable() -> bool:
    """开启自启，返回是否成功。"""
    try:
        if is_windows():
            return _win_set(True)
        if is_macos():
            return _mac_set(True)
    except Exception as e:
        print(f"设置开机自启失败: {e}")
        return False
    return False


def disable() -> bool:
    """关闭自启，返回是否成功。"""
    try:
        if is_windows():
            return _win_set(False)
        if is_macos():
            return _mac_set(False)
    except Exception as e:
        print(f"取消开机自启失败: {e}")
        return False
    return False


# ----------------------------- Windows -----------------------------

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_is_enabled() -> bool:
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False


def _win_set(enable_it: bool) -> bool:
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_WRITE
    )
    try:
        if enable_it:
            cmd = _start_command()
            # 注册表 Run 项需要单一字符串命令
            command = " ".join(f'"{c}"' for c in cmd)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    return True


# ----------------------------- macOS -----------------------------

def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_BUNDLE_ID}.plist"


def _mac_plist_content() -> str:
    cmd = _start_command()
    # 简单转义命令参数中的双引号与 &（参数为路径，正常不含这些）
    args_xml = "".join(f"<string>{_xml_escape(c)}</string>" for c in cmd)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MAC_BUNDLE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        {args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _mac_set(enable_it: bool) -> bool:
    plist = _mac_plist_path()
    if enable_it:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(_mac_plist_content(), encoding="utf-8")
        # 尝试用 launchctl 加载，失败也无所谓——下次登录会自动加载
        try:
            import subprocess
            subprocess.run(
                ["launchctl", "load", str(plist)],
                check=False, capture_output=True,
            )
        except Exception:
            pass
    else:
        if plist.exists():
            try:
                import subprocess
                subprocess.run(
                    ["launchctl", "unload", str(plist)],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
            plist.unlink()
    return True
