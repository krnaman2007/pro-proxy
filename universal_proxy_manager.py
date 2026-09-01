"""
Universal App Proxy Manager for Windows 10 & 11.
Provides transparent redirection of Windows application network traffic through SOCKS5/HTTP proxies
using high-performance native engines (sing-box and tun2socks) with Wintun driver support.

Architecture:
  Python GUI / Controller -> Background Worker -> Native Engine (sing-box/tun2socks) -> Windows Traffic -> SOCKS5 -> Internet
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

TUN_INTERFACE_NAME = "wintun"
TUN_IP = "10.255.0.2"
TUN_GATEWAY = "10.255.0.1"
TUN_NETMASK = "255.255.255.0"

# State Constants
STATE_STOPPED = "STOPPED"
STATE_CONNECTING = "CONNECTING"
STATE_ACTIVE = "ACTIVE"
STATE_FAILED = "FAILED"
STATE_STOPPING = "STOPPING"

# Thread-safe global state
_state_lock = threading.Lock()
_current_state: str = STATE_STOPPED
_last_error: str = ""
_current_proxy_target: str = ""
_current_proxy_type: str = "socks5"
_current_engine_name: str = "sing-box"

_engine_process: Optional[subprocess.Popen] = None
_active_host_route: Optional[str] = None
_physical_gateway: Optional[str] = None
_active_config_file: Optional[str] = None
_worker_thread: Optional[threading.Thread] = None


def is_admin() -> bool:
    """Checks if the current process has Windows Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin(arguments: str = "") -> bool:
    """Relaunches the application with UAC Administrator elevation."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if getattr(sys, "frozen", False):
            target_exe = sys.executable
            params = arguments
            work_dir = os.path.dirname(target_exe)
        else:
            target_exe = sys.executable
            main_script = os.path.join(base_dir, "main.py")
            if not os.path.exists(main_script):
                main_script = os.path.abspath(sys.argv[0])
            params = f'"{main_script}" {arguments}'.strip()
            work_dir = base_dir

        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            target_exe,
            params,
            work_dir,
            1  # SW_SHOWNORMAL
        )
        return int(ret) > 32
    except Exception as error:
        print(f"[UniversalProxy] Failed to request UAC elevation: {error}")
        return False


def get_binary_paths() -> Dict[str, Optional[str]]:
    """
    Finds the absolute paths to sing-box.exe, tun2socks.exe, and wintun.dll.
    Supports development directory, ./bin subfolder, and PyInstaller bundled directory.
    """
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
        candidates.append(os.path.join(sys._MEIPASS, "bin"))

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(base_dir)
    candidates.append(os.path.join(base_dir, "bin"))

    paths: Dict[str, Optional[str]] = {
        "sing_box": None,
        "tun2socks": None,
        "wintun": None
    }

    for folder in candidates:
        sb = os.path.join(folder, "sing-box.exe")
        t2s = os.path.join(folder, "tun2socks.exe")
        w_dll = os.path.join(folder, "wintun.dll")

        if os.path.exists(sb) and not paths["sing_box"]:
            paths["sing_box"] = sb
        if os.path.exists(t2s) and not paths["tun2socks"]:
            paths["tun2socks"] = t2s
        if os.path.exists(w_dll) and not paths["wintun"]:
            paths["wintun"] = w_dll

    return paths


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
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                if gw != "On-link" and not gw.startswith("10.255.") and not gw.startswith("172.19."):
                    return gw
    except Exception as error:
        print(f"[UniversalProxy] Could not detect physical gateway: {error}")
    return None


def is_engine_running() -> bool:
    """Returns True if the proxy engine process is actively running."""
    global _engine_process
    with _state_lock:
        if _engine_process is not None:
            if _engine_process.poll() is None:
                return True
            _engine_process = None
    return False


def get_universal_proxy_status() -> Dict[str, Any]:
    """Returns real-time status and diagnostics of Universal App Proxy Mode."""
    with _state_lock:
        running = is_engine_running()
        state = _current_state
        if running and state != STATE_ACTIVE:
            state = STATE_ACTIVE
        elif not running and state == STATE_ACTIVE:
            state = STATE_STOPPED

        return {
            "state": state,
            "enabled": (state == STATE_ACTIVE),
            "mode": "universal",
            "engine": _current_engine_name if (state == STATE_ACTIVE) else "",
            "proxy_target": _current_proxy_target if (state in (STATE_ACTIVE, STATE_CONNECTING)) else "",
            "proxy_type": _current_proxy_type if (state in (STATE_ACTIVE, STATE_CONNECTING)) else "",
            "last_error": _last_error,
            "is_admin": is_admin()
        }


def _generate_sing_box_config(ip: str, port: int, proxy_type: str) -> Dict[str, Any]:
    """Generates a sing-box JSON configuration for auto-routing and DNS safety."""
    outbound_type = "socks" if proxy_type == "socks5" else "http"
    return {
        "log": {
            "level": "warn"
        },
        "dns": {
            "servers": [
                {
                    "type": "local",
                    "tag": "local-dns",
                    "detour": "direct"
                }
            ]
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": TUN_INTERFACE_NAME,
                "address": ["172.19.0.1/30"],
                "auto_route": True,
                "strict_route": False,
                "stack": "system"
            }
        ],
        "outbounds": [
            {
                "type": outbound_type,
                "tag": "proxy-out",
                "server": ip,
                "server_port": port
            },
            {
                "type": "direct",
                "tag": "direct"
            }
        ],
        "route": {
            "auto_detect_interface": True,
            "rules": [
                {
                    "protocol": "dns",
                    "outbound": "direct"
                },
                {
                    "ip_is_private": True,
                    "outbound": "direct"
                }
            ]
        }
    }


def _internal_start_proxy(ip: str, port: int | str, proxy_type: str = "socks5") -> Tuple[bool, str]:
    """
    Internal synchronous worker function that manages process launching and driver configuration.
    Must be called from a background worker thread to keep the GUI 100% responsive.
    """
    global _engine_process, _current_proxy_target, _current_proxy_type, _current_engine_name
    global _active_host_route, _physical_gateway, _active_config_file, _current_state, _last_error

    ip = str(ip).strip()
    try:
        port_num = int(str(port).strip())
    except ValueError:
        with _state_lock:
            _current_state = STATE_FAILED
            _last_error = f"Invalid port number: '{port}'"
        return False, _last_error

    proxy_type = proxy_type.lower().strip()
    if proxy_type not in ("socks5", "http"):
        proxy_type = "socks5"

    if not is_admin():
        with _state_lock:
            _current_state = STATE_FAILED
            _last_error = "Administrator privileges are required to configure the network TUN driver."
        return False, _last_error

    binaries = get_binary_paths()
    sing_box_exe = binaries.get("sing_box")
    tun2socks_exe = binaries.get("tun2socks")
    wintun_dll = binaries.get("wintun")

    if not sing_box_exe and not tun2socks_exe:
        with _state_lock:
            _current_state = STATE_FAILED
            _last_error = "No proxy engine found (sing-box.exe or tun2socks.exe). Please verify installation."
        return False, _last_error

    with _state_lock:
        if is_engine_running():
            _internal_stop_proxy()

        _current_state = STATE_CONNECTING
        _current_proxy_target = f"{ip}:{port_num}"
        _current_proxy_type = proxy_type
        _last_error = ""

    # Clean any lingering engine processes before launching
    try:
        subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.3)
    except Exception:
        pass

    wintun_dir = os.path.dirname(wintun_dll) if wintun_dll else os.getcwd()
    env = os.environ.copy()
    if wintun_dir:
        env["PATH"] = f"{wintun_dir};" + env.get("PATH", "")

    # -------------------------------------------------------------
    # 1. Primary Engine: sing-box
    # -------------------------------------------------------------
    if sing_box_exe and os.path.exists(sing_box_exe):
        try:
            cfg_data = _generate_sing_box_config(ip, port_num, proxy_type)
            cfg_fd, cfg_path = tempfile.mkstemp(prefix="proproxy_singbox_", suffix=".json")
            with open(cfg_fd, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, indent=2)

            with _state_lock:
                _active_config_file = cfg_path

            cmd = [sing_box_exe, "run", "-c", cfg_path]
            proc = subprocess.Popen(
                cmd,
                cwd=wintun_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            # Allow native engine time to load driver and bind interface
            time.sleep(1.2)
            if proc.poll() is not None:
                _, err = proc.communicate()
                err_msg = err.decode("utf-8", errors="ignore").strip() if err else "sing-box exited immediately."
                print(f"[UniversalProxy] sing-box startup failed: {err_msg}. Trying tun2socks fallback...")
            else:
                with _state_lock:
                    _engine_process = proc
                    _current_state = STATE_ACTIVE
                    _current_engine_name = "sing-box"
                    _last_error = ""
                return True, f"Universal App Proxy active [sing-box] ({proxy_type.upper()} -> {ip}:{port_num})"

        except Exception as error:
            print(f"[UniversalProxy] Exception starting sing-box: {error}")

    # -------------------------------------------------------------
    # 2. Fallback Engine: tun2socks
    # -------------------------------------------------------------
    if tun2socks_exe and os.path.exists(tun2socks_exe):
        _physical_gateway = get_physical_gateway()
        proxy_url = f"{proxy_type}://{ip}:{port_num}"

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
                print(f"[UniversalProxy] Host route note: {e}")

        cmd = [
            tun2socks_exe,
            "--device", f"tun://{TUN_INTERFACE_NAME}",
            "--proxy", proxy_url,
            "--loglevel", "warn"
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=wintun_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            time.sleep(1.2)
            if proc.poll() is not None:
                _, err = proc.communicate()
                err_msg = err.decode("utf-8", errors="ignore").strip() if err else "tun2socks exited immediately."
                with _state_lock:
                    _current_state = STATE_FAILED
                    _last_error = f"Engine failed: {err_msg}"
                return False, _last_error

            # Setup TUN IP and routes
            try:
                subprocess.run(
                    ["netsh", "interface", "ip", "set", "address", f"name={TUN_INTERFACE_NAME}", "static", TUN_IP, TUN_NETMASK, TUN_GATEWAY],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
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
            except Exception as error:
                print(f"[UniversalProxy] Routing notice: {error}")

            with _state_lock:
                _engine_process = proc
                _current_state = STATE_ACTIVE
                _current_engine_name = "tun2socks"
                _last_error = ""
            return True, f"Universal App Proxy active [tun2socks] ({proxy_type.upper()} -> {ip}:{port_num})"

        except Exception as error:
            with _state_lock:
                _current_state = STATE_FAILED
                _last_error = f"Failed to launch tun2socks: {error}"
            return False, _last_error

    with _state_lock:
        _current_state = STATE_FAILED
        _last_error = "Unable to start Universal Proxy engine."
    return False, _last_error


def _internal_stop_proxy() -> Tuple[bool, str]:
    """
    Internal synchronous worker function to terminate engine and restore routing.
    """
    global _engine_process, _current_proxy_target, _current_proxy_type
    global _active_host_route, _active_config_file, _current_state, _last_error

    with _state_lock:
        _current_state = STATE_STOPPING

    # 1. Clean up manual route entries
    try:
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if _active_host_route:
            subprocess.run(["route", "delete", _active_host_route], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            _active_host_route = None
    except Exception:
        pass

    # 2. Terminate engine process
    with _state_lock:
        if _engine_process is not None:
            try:
                _engine_process.terminate()
                _engine_process.wait(timeout=1.5)
            except Exception:
                try:
                    _engine_process.kill()
                except Exception:
                    pass
            _engine_process = None

    # 3. Kill any leftover engine processes
    try:
        subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass

    # 4. Remove temporary config file
    with _state_lock:
        if _active_config_file and os.path.exists(_active_config_file):
            try:
                os.remove(_active_config_file)
            except Exception:
                pass
            _active_config_file = None

        _current_state = STATE_STOPPED
        _current_proxy_target = ""
        _current_proxy_type = "socks5"
        _last_error = ""

    return True, "Universal App Proxy stopped."


# =============================================================================
# ASYNCHRONOUS NON-BLOCKING CONTROLLER API
# =============================================================================

def start_universal_proxy_async(
    ip: str,
    port: int | str,
    proxy_type: str = "socks5",
    on_complete: Optional[Callable[[bool, str], None]] = None
) -> None:
    """
    Non-blocking asynchronous trigger to start Universal App Proxy.
    Immediately returns to ensure GUI remains 100% fluid and responsive.
    Calls on_complete(success, message) upon completion.
    """
    global _worker_thread, _current_state, _last_error, _current_proxy_target, _current_proxy_type

    with _state_lock:
        _current_state = STATE_CONNECTING
        _current_proxy_target = f"{ip}:{port}"
        _current_proxy_type = proxy_type
        _last_error = ""

    def _worker():
        success, msg = _internal_start_proxy(ip, port, proxy_type)
        if on_complete:
            try:
                on_complete(success, msg)
            except Exception as e:
                print(f"[UniversalProxy] Callback error: {e}")

    _worker_thread = threading.Thread(target=_worker, daemon=True, name="UniversalProxyStarter")
    _worker_thread.start()


def stop_universal_proxy_async(
    on_complete: Optional[Callable[[bool, str], None]] = None
) -> None:
    """
    Non-blocking asynchronous trigger to stop Universal App Proxy.
    Immediately returns to ensure GUI remains 100% fluid and responsive.
    Calls on_complete(success, message) upon completion.
    """
    global _worker_thread

    def _worker():
        success, msg = _internal_stop_proxy()
        if on_complete:
            try:
                on_complete(success, msg)
            except Exception as e:
                print(f"[UniversalProxy] Callback error: {e}")

    _worker_thread = threading.Thread(target=_worker, daemon=True, name="UniversalProxyStopper")
    _worker_thread.start()


def start_universal_proxy(ip: str, port: int | str, proxy_type: str = "socks5") -> Tuple[bool, str]:
    """Synchronous wrapper for starting universal proxy."""
    return _internal_start_proxy(ip, port, proxy_type)


def stop_universal_proxy() -> Tuple[bool, str]:
    """Synchronous wrapper for stopping universal proxy."""
    return _internal_stop_proxy()


def cleanup_on_startup():
    """Performs safety cleanup of any leftover routes or processes from abnormal terminations."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
