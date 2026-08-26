"""
Proxy Manager for Windows 10 and 11.
Interacts with the Windows Registry under:
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
And notifies the WinINet subsystem via ctypes for immediate effect.
"""

import ctypes
import winreg
from typing import Dict, Tuple

# Windows Registry Path for Internet Settings
INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# WinINet Options for broadcasting proxy changes
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


def refresh_wininet_cache() -> bool:
    """
    Notifies Windows and running browsers (Edge, Chrome, etc.)
    that proxy settings have changed so they take effect immediately
    without requiring a restart.
    """
    try:
        wininet = ctypes.windll.wininet
        # Notify that settings have changed
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        # Refresh the settings cache
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
        return True
    except Exception as error:
        print(f"[ProxyManager] Warning: Failed to refresh WinINet cache: {error}")
        return False


def get_current_proxy_status() -> Dict[str, any]:
    """
    Reads the current system proxy settings from the Windows Registry.
    Returns a dictionary containing:
        - "enabled": bool (True if ProxyEnable == 1)
        - "server": str (e.g. "127.0.0.1:8080" or empty)
        - "override": str (e.g. "<local>" or empty)
        - "success": bool
        - "error": str | None
    """
    result = {
        "enabled": False,
        "server": "",
        "override": "",
        "success": False,
        "error": None
    }

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, winreg.KEY_READ) as key:
            try:
                proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                result["enabled"] = bool(proxy_enable)
            except FileNotFoundError:
                result["enabled"] = False

            try:
                proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                result["server"] = str(proxy_server)
            except FileNotFoundError:
                result["server"] = ""

            try:
                proxy_override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                result["override"] = str(proxy_override)
            except FileNotFoundError:
                result["override"] = ""

            result["success"] = True
    except Exception as error:
        result["error"] = str(error)
        print(f"[ProxyManager] Error reading registry: {error}")

    return result


def enable_proxy(ip: str, port: int | str) -> Tuple[bool, str]:
    """
    Enables Windows system proxy with the given IP and port.
    Sets ProxyEnable = 1 and ProxyServer = "IP:PORT" in HKCU Internet Settings.
    Returns (success: bool, message: str).
    """
    ip = str(ip).strip()
    port = str(port).strip()
    proxy_address = f"{ip}:{port}"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INTERNET_SETTINGS_KEY,
            0,
            winreg.KEY_SET_VALUE
        ) as key:
            # Set ProxyEnable to 1 (DWORD)
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)

            # Set ProxyServer to "IP:PORT" (REG_SZ)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_address)

        # Notify WinINet subsystem to apply changes immediately
        refresh_wininet_cache()
        return True, f"Proxy successfully enabled: {proxy_address}"

    except PermissionError:
        return False, "Permission denied while writing to Windows Registry."
    except Exception as error:
        return False, f"Failed to enable proxy: {str(error)}"


def disable_proxy() -> Tuple[bool, str]:
    """
    Disables Windows system proxy by setting ProxyEnable = 0.
    Returns (success: bool, message: str).
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INTERNET_SETTINGS_KEY,
            0,
            winreg.KEY_SET_VALUE
        ) as key:
            # Set ProxyEnable to 0 (DWORD)
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)

        # Notify WinINet subsystem to apply changes immediately
        refresh_wininet_cache()
        return True, "Proxy successfully disabled."

    except PermissionError:
        return False, "Permission denied while writing to Windows Registry."
    except Exception as error:
        return False, f"Failed to disable proxy: {str(error)}"


def toggle_proxy(enable: bool, ip: str = "", port: int | str = "") -> Tuple[bool, str]:
    """
    Helper function to toggle proxy state on or off.
    """
    if enable:
        return enable_proxy(ip, port)
    else:
        return disable_proxy()
