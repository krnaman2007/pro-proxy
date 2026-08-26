"""
Universal App Proxy Manager for Windows 10 & 11.
Redirects network traffic from all Windows applications through SOCKS5/HTTP proxies
using the open-source tun2socks engine and Wintun driver.
"""

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

TUN_INTERFACE_NAME = "wintun"
TUN_IP = "10.255.0.2"
TUN_GATEWAY = "10.255.0.1"
TUN_NETMASK = "255.255.255.0"

_engine_process: Optional[subprocess.Popen] = None
_engine_lock = threading.Lock()
_current_proxy_target: str = ""
_current_proxy_type: str = "socks5"
_active_host_route: Optional[str] = None
_physical_gateway: Optional[str] = None


def is_admin() -> bool:
    """Checks if the current process has Windows Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin(arguments: str = "") -> bool:
    """Relaunches the application with UAC Administrator elevation."""
    try:
        if getattr(sys, "frozen", False):
            target_exe = sys.executable
            params = arguments
        else:
            target_exe = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            params = f'"{script_path}" {arguments}'.strip()

        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            target_exe,
            params,
            None,
            1  # SW_SHOWNORMAL
        )
        return int(ret) > 32
    except Exception as error:
        print(f"[UniversalProxy] Failed to request UAC elevation: {error}")
        return False


def get_binary_paths() -> Tuple[Optional[str], Optional[str]]:
    """
    Finds the absolute paths to tun2socks.exe and wintun.dll.
    Supports development directory, ./bin subfolder, and PyInstaller bundled directory.
    """
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
        candidates.append(os.path.join(sys._MEIPASS, "bin"))

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(base_dir)
    candidates.append(os.path.join(base_dir, "bin"))

    tun2socks_path = None
    wintun_path = None

    for folder in candidates:
        t_exe = os.path.join(folder, "tun2socks.exe")
        w_dll = os.path.join(folder, "wintun.dll")
        if os.path.exists(t_exe) and not tun2socks_path:
            tun2socks_path = t_exe
        if os.path.exists(w_dll) and not wintun_path:
            wintun_path = w_dll

    return tun2socks_path, wintun_path


def get_physical_gateway() -> Optional[str]:
    """Retrieves the default IPv4 gateway of the primary network adapter."""
    try:
        output = subprocess.check_output(
            ["route", "print", "0.0.0.0"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in output.splitlines():
            line = line.strip()
            # Match 0.0.0.0 0.0.0.0 <Gateway> <Interface>
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                if gw != "On-link" and not gw.startswith("10.255."):
                    return gw
    except Exception as error:
        print(f"[UniversalProxy] Could not detect physical gateway: {error}")
    return None


def is_engine_running() -> bool:
    """Returns True if the tun2socks engine process is actively running."""
    global _engine_process
    with _engine_lock:
        if _engine_process is not None:
            if _engine_process.poll() is None:
                return True
            _engine_process = None
    return False


def get_universal_proxy_status() -> Dict[str, Any]:
    """Returns diagnostic status dictionary of Universal App Proxy Mode."""
    running = is_engine_running()
    return {
        "enabled": running,
        "mode": "universal",
        "proxy_target": _current_proxy_target if running else "",
        "proxy_type": _current_proxy_type if running else "",
        "is_admin": is_admin()
    }


def start_universal_proxy(ip: str, port: int | str, proxy_type: str = "socks5") -> Tuple[bool, str]:
    """
    Starts the Universal App Proxy redirection engine.
    Redirects all system and application TCP/UDP traffic through the proxy.
    """
    global _engine_process, _current_proxy_target, _current_proxy_type, _active_host_route, _physical_gateway

    ip = str(ip).strip()
    port = str(port).strip()
    proxy_type = proxy_type.lower().strip()
    if proxy_type not in ("socks5", "http"):
        proxy_type = "socks5"

    if not is_admin():
        return False, "Administrator privileges are required to configure the network TUN interface."

    tun2socks_exe, wintun_dll = get_binary_paths()
    if not tun2socks_exe or not os.path.exists(tun2socks_exe):
        return False, "tun2socks.exe binary not found. Please verify application installation."
    if not wintun_dll or not os.path.exists(wintun_dll):
        return False, "wintun.dll driver not found. Please verify application installation."

    with _engine_lock:
        if is_engine_running():
            stop_universal_proxy()

        # Kill any orphaned tun2socks processes
        try:
            subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(0.5)
        except Exception:
            pass

        # Detect physical gateway for loop prevention
        _physical_gateway = get_physical_gateway()
        proxy_url = f"{proxy_type}://{ip}:{port}"

        # If proxy is an IP, add direct route via physical gateway to prevent infinite routing loop
        is_ipv4 = bool(re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip))
        if is_ipv4 and _physical_gateway:
            try:
                subprocess.run(
                    ["route", "add", ip, "mask", "255.255.255.255", _physical_gateway, "metric", "1"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                _active_host_route = ip
            except Exception as e:
                print(f"[UniversalProxy] Warning: Could not add direct proxy host route: {e}")

        # Ensure wintun.dll directory is in PATH so tun2socks can load it
        wintun_dir = os.path.dirname(wintun_dll)
        env = os.environ.copy()
        env["PATH"] = f"{wintun_dir};" + env.get("PATH", "")

        # Launch tun2socks
        cmd = [
            tun2socks_exe,
            "-device", f"tun://{TUN_INTERFACE_NAME}",
            "-proxy", proxy_url,
            "-loglevel", "warning"
        ]

        try:
            _engine_process = subprocess.Popen(
                cmd,
                cwd=wintun_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception as error:
            return False, f"Failed to start tun2socks engine: {error}"

        # Wait for TUN adapter to initialize
        time.sleep(1.5)
        if _engine_process.poll() is not None:
            _, err = _engine_process.communicate()
            err_msg = err.decode("utf-8", errors="ignore").strip() if err else "Engine terminated unexpectedly."
            _engine_process = None
            return False, f"tun2socks failed to start: {err_msg}"

        # Configure IP address and routes on the virtual interface
        try:
            # 1. Assign static IP to TUN adapter
            subprocess.run(
                ["netsh", "interface", "ip", "set", "address", f"name={TUN_INTERFACE_NAME}", "static", TUN_IP, TUN_NETMASK, TUN_GATEWAY],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            # 2. Add high-priority split-default routes through TUN gateway
            subprocess.run(
                ["route", "add", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY, "metric", "1"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            subprocess.run(
                ["route", "add", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY, "metric", "1"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            # 3. Configure DNS on the TUN adapter (Google & Cloudflare DNS)
            subprocess.run(
                ["netsh", "interface", "ip", "set", "dns", f"name={TUN_INTERFACE_NAME}", "static", "8.8.8.8"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            subprocess.run(
                ["netsh", "interface", "ip", "add", "dns", f"name={TUN_INTERFACE_NAME}", "1.1.1.1", "index=2"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

        except Exception as error:
            print(f"[UniversalProxy] Notice during routing setup: {error}")

        _current_proxy_target = f"{ip}:{port}"
        _current_proxy_type = proxy_type
        return True, f"Universal App Proxy active ({proxy_type.upper()} -> {ip}:{port})"


def stop_universal_proxy() -> Tuple[bool, str]:
    """Stops the Universal App Proxy engine and restores default routing."""
    global _engine_process, _current_proxy_target, _current_proxy_type, _active_host_route

    with _engine_lock:
        # 1. Remove custom routing table entries
        try:
            subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if _active_host_route:
                subprocess.run(["route", "delete", _active_host_route], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                _active_host_route = None
        except Exception as error:
            print(f"[UniversalProxy] Route cleanup notice: {error}")

        # 2. Terminate the tun2socks process
        if _engine_process is not None:
            try:
                _engine_process.terminate()
                _engine_process.wait(timeout=2.0)
            except Exception:
                try:
                    _engine_process.kill()
                except Exception:
                    pass
            _engine_process = None

        # 3. Force kill any stray tun2socks processes
        try:
            subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

        _current_proxy_target = ""
        _current_proxy_type = "socks5"
        return True, "Universal App Proxy stopped."


def cleanup_on_startup():
    """Performs cleanup of any leftover routes or processes from an abnormal termination."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
