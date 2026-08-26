"""
Auto-Start Manager for Windows 10 and 11.
Manages automatic startup on user login via the Windows Registry:
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
"""

import os
import sys
import winreg
from typing import Tuple

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_REG_NAME = "ProProxy"


def get_startup_command() -> str:
    """
    Constructs the command to execute on Windows login.
    Supports both frozen PyInstaller executable and python script mode.
    Adds '--minimized' flag so the app starts directly to the system tray.
    """
    if getattr(sys, "frozen", False):
        # Running as compiled .exe (PyInstaller)
        exe_path = sys.executable
        return f'"{exe_path}" --minimized'
    else:
        # Running as python script
        # Prefer pythonw.exe if available to avoid opening a console window on boot
        executable = sys.executable
        pythonw_candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(pythonw_candidate):
            executable = pythonw_candidate

        main_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
        return f'"{executable}" "{main_script}" --minimized'


def is_autostart_enabled() -> bool:
    """
    Checks if ProProxy is registered in HKCU Run key.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
                return bool(val)
            except FileNotFoundError:
                return False
    except Exception as error:
        print(f"[AutoStartManager] Error checking autostart: {error}")
        return False


def set_autostart(enable: bool) -> Tuple[bool, str]:
    """
    Enables or disables automatic startup on Windows login.
    Returns (success: bool, message: str).
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                cmd = get_startup_command()
                winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd)
                return True, f"Startup enabled: {cmd}"
            else:
                try:
                    winreg.DeleteValue(key, APP_REG_NAME)
                    return True, "Startup disabled."
                except FileNotFoundError:
                    return True, "Startup was already disabled."
    except PermissionError:
        return False, "Permission denied writing to Windows Run registry key."
    except Exception as error:
        return False, f"Failed to modify startup registry: {str(error)}"
