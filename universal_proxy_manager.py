"""
Universal App Proxy Manager for Windows 10 & 11.
Redirects all Windows application network traffic through SOCKS5/HTTP proxies
using high-performance sing-box and tun2socks transparent proxy engines.
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
from typing import Any, Dict, Optional, Tuple

TUN_INTERFACE_NAME = "wintun"
TUN_IP = "10.255.0.2"
TUN_GATEWAY = "10.255.0.1"
TUN_NETMASK = "255.255.255.0"

_engine_process: Optional[subprocess.Popen] = None
_engine_lock = threading.Lock()
_current_proxy_target: str = ""
_current_proxy_type: str = "socks5"
_current_engine_name: str = "sing-box"
_active_host_route: Optional[str] = None
_physical_gateway: Optional[str] = None
_active_config_file: Optional[str] = None


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
        "engine": _current_engine_name if running else "",
        "proxy_target": _current_proxy_target if running else "",
        "proxy_type": _current_proxy_type if running else "",
        "is_admin": is_admin()
    }


def _generate_sing_box_config(ip: str, port: int, proxy_type: str) -> Dict[str, Any]:
    """Generates a high-performance sing-box JSON configuration for transparent routing."""
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


def start_universal_proxy(ip: str, port: int | str, proxy_type: str = "socks5") -> Tuple[bool, str]:
    """
    Starts the Universal App Proxy redirection engine.
    Redirects all Windows applications' network traffic through the configured proxy.
    """
    global _engine_process, _current_proxy_target, _current_proxy_type, _current_engine_name
    global _active_host_route, _physical_gateway, _active_config_file

    ip = str(ip).strip()
    try:
        port_num = int(str(port).strip())
    except ValueError:
        return False, f"Invalid port number: '{port}'"

    proxy_type = proxy_type.lower().strip()
    if proxy_type not in ("socks5", "http"):
        proxy_type = "socks5"

    if not is_admin():
        return False, "Administrator privileges are required to configure the network TUN driver."

    binaries = get_binary_paths()
    sing_box_exe = binaries.get("sing_box")
    tun2socks_exe = binaries.get("tun2socks")
    wintun_dll = binaries.get("wintun")

    if not sing_box_exe and not tun2socks_exe:
        return False, "No proxy engine found (sing-box.exe or tun2socks.exe). Please reinstall the app."

    with _engine_lock:
        if is_engine_running():
            stop_universal_proxy()

        # Kill any orphaned processes from prior sessions
        try:
            subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(0.4)
        except Exception:
            pass

        wintun_dir = os.path.dirname(wintun_dll) if wintun_dll else os.getcwd()
        env = os.environ.copy()
        if wintun_dir:
            env["PATH"] = f"{wintun_dir};" + env.get("PATH", "")

        # -------------------------------------------------------------
        # OPTION 1: Primary Engine - sing-box (Seamless auto-routing)
        # -------------------------------------------------------------
        if sing_box_exe and os.path.exists(sing_box_exe):
            try:
                cfg_data = _generate_sing_box_config(ip, port_num, proxy_type)
                cfg_fd, cfg_path = tempfile.mkstemp(prefix="proproxy_singbox_", suffix=".json")
                with open(cfg_fd, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, indent=2)

                _active_config_file = cfg_path

                cmd = [sing_box_exe, "run", "-c", cfg_path]
                _engine_process = subprocess.Popen(
                    cmd,
                    cwd=wintun_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )

                time.sleep(1.5)
                if _engine_process.poll() is not None:
                    _, err = _engine_process.communicate()
                    err_msg = err.decode("utf-8", errors="ignore").strip() if err else "sing-box exited immediately."
                    _engine_process = None
                    # If sing-box failed, fallback to tun2socks
                    print(f"[UniversalProxy] sing-box startup error: {err_msg}. Attempting tun2socks fallback...")
                else:
                    _current_proxy_target = f"{ip}:{port_num}"
                    _current_proxy_type = proxy_type
                    _current_engine_name = "sing-box"
                    return True, f"Universal App Proxy active [sing-box] ({proxy_type.upper()} -> {ip}:{port_num})"

            except Exception as error:
                print(f"[UniversalProxy] Failed to launch sing-box: {error}. Falling back...")

        # -------------------------------------------------------------
        # OPTION 2: Fallback Engine - tun2socks (v2.7.0)
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

            time.sleep(1.5)
            if _engine_process.poll() is not None:
                _, err = _engine_process.communicate()
                err_msg = err.decode("utf-8", errors="ignore").strip() if err else "tun2socks exited immediately."
                _engine_process = None
                return False, f"Proxy engine failed to start: {err_msg}"

            # Setup TUN IP and split routes
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

            _current_proxy_target = f"{ip}:{port_num}"
            _current_proxy_type = proxy_type
            _current_engine_name = "tun2socks"
            return True, f"Universal App Proxy active [tun2socks] ({proxy_type.upper()} -> {ip}:{port_num})"

        return False, "Unable to initialize any proxy engine."


def stop_universal_proxy() -> Tuple[bool, str]:
    """Stops the Universal App Proxy engine and restores system routing."""
    global _engine_process, _current_proxy_target, _current_proxy_type, _active_host_route, _active_config_file

    with _engine_lock:
        # 1. Clean up manual route table entries
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

        # 3. Kill any stray engine processes
        try:
            subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

        # 4. Clean up temporary config file
        if _active_config_file and os.path.exists(_active_config_file):
            try:
                os.remove(_active_config_file)
            except Exception:
                pass
            _active_config_file = None

        _current_proxy_target = ""
        _current_proxy_type = "socks5"
        return True, "Universal App Proxy stopped."


def cleanup_on_startup():
    """Cleans up any leftover routes or processes from abnormal terminations."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["taskkill", "/F", "/IM", "tun2socks.exe"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GATEWAY], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        subprocess.run(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", "172.19.0.1"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
