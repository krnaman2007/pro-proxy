"""
Network Monitor Module for Windows.
Detects whether the active internet connection is routed through an Ethernet adapter or Wi-Fi.
Provides a background polling worker with state tracking to avoid redundant proxy toggles.
"""

import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import psutil
import wifi_manager


def get_active_network_info() -> Dict[str, Any]:
    """
    Determines the currently active network interface used for outbound internet routing.
    Returns dictionary with:
        - "type": "ethernet" | "wifi" | "other" | "disconnected"
        - "interface_name": str | None
        - "ip_address": str | None
        - "is_ethernet": bool
        - "ssid": str | None (if Wi-Fi)
    """
    # 1. Determine which local IP Windows is using to reach the internet
    active_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.8)
        s.connect(("8.8.8.8", 80))
        active_ip = s.getsockname()[0]
        s.close()
    except Exception:
        # Fallback to local default gateway interface
        pass

    if not active_ip:
        # Check if any network interface is UP
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for name, stat in stats.items():
            if stat.isup and name in addrs:
                for a in addrs[name]:
                    if a.family == socket.AF_INET and not a.address.startswith("127.") and not a.address.startswith("169.254."):
                        active_ip = a.address
                        break
                if active_ip:
                    break

    if not active_ip:
        return {
            "type": "disconnected",
            "interface_name": None,
            "ip_address": None,
            "is_ethernet": False,
            "ssid": None
        }

    # 2. Match active IP with network interface name and type
    matched_name = None
    addrs = psutil.net_if_addrs()
    for name, addr_list in addrs.items():
        for addr in addr_list:
            if addr.family == socket.AF_INET and addr.address == active_ip:
                matched_name = name
                break
        if matched_name:
            break

    if not matched_name:
        matched_name = "Unknown"

    name_lower = matched_name.lower()
    is_wifi = any(w in name_lower for w in ("wi-fi", "wifi", "wireless", "wlan", "802.11"))
    is_eth = any(e in name_lower for e in ("ethernet", "eth", "lan", "local area connection", "gigabit", "realtek", "intel(r) ethernet")) and not is_wifi

    if is_eth:
        iface_type = "ethernet"
    elif is_wifi:
        iface_type = "wifi"
    else:
        iface_type = "other"

    ssid = None
    if iface_type == "wifi":
        wifi_info = wifi_manager.get_current_wifi()
        if wifi_info.get("connected"):
            ssid = wifi_info.get("ssid")

    return {
        "type": iface_type,
        "interface_name": matched_name,
        "ip_address": active_ip,
        "is_ethernet": (iface_type == "ethernet"),
        "ssid": ssid
    }


class EthernetProxyWorker:
    """
    Standalone background worker thread that polls network interfaces every 5 seconds.
    Triggers enable_proxy() when Ethernet is active and disable_proxy() when Ethernet drops.
    Tracks state to prevent redundant repeated calls every 5 seconds.
    """

    def __init__(
        self,
        enable_proxy_fn: Callable[[], Any],
        disable_proxy_fn: Callable[[], Any],
        poll_interval: float = 5.0,
        on_state_change: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ):
        self.enable_proxy_fn = enable_proxy_fn
        self.disable_proxy_fn = disable_proxy_fn
        self.poll_interval = poll_interval
        self.on_state_change = on_state_change

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_proxy_enabled = False
        self._last_interface_type: Optional[str] = None

    def start(self):
        """Starts the background polling worker."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="EthernetProxyWorker")
        self._thread.start()

    def stop(self):
        """Stops the background polling worker."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def set_proxy_state(self, enabled: bool):
        """Manually updates the known proxy state to prevent out-of-sync triggers."""
        self._is_proxy_enabled = enabled

    def _run_loop(self):
        """Background polling loop executed every 5 seconds."""
        # Initial immediate check
        self._check_and_update()

        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.poll_interval):
                break
            if self._running and not self._stop_event.is_set():
                self._check_and_update()

    def _check_and_update(self):
        """Checks network adapter state and triggers proxy enable/disable accordingly."""
        net_info = get_active_network_info()
        current_type = net_info["type"]
        is_ethernet = net_info["is_ethernet"]

        # If active connection is Ethernet
        if is_ethernet:
            if not self._is_proxy_enabled:
                print(f"[EthernetWorker] Ethernet detected ({net_info['interface_name']}) -> Enabling proxy...")
                self._is_proxy_enabled = True
                try:
                    self.enable_proxy_fn()
                except Exception as e:
                    print(f"[EthernetWorker] Error calling enable_proxy: {e}")

                if self.on_state_change:
                    self.on_state_change("ethernet_enabled", net_info)
        else:
            # Not Ethernet (switched to Wi-Fi, other, or disconnected)
            if self._is_proxy_enabled:
                print(f"[EthernetWorker] Ethernet dropped (current: {current_type}) -> Disabling proxy...")
                self._is_proxy_enabled = False
                try:
                    self.disable_proxy_fn()
                except Exception as e:
                    print(f"[EthernetWorker] Error calling disable_proxy: {e}")

                if self.on_state_change:
                    self.on_state_change("ethernet_disabled", net_info)

        self._last_interface_type = current_type


# Example standalone test execution
if __name__ == "__main__":
    def dummy_enable():
        print(">>> [DUMMY] enable_proxy() executed")

    def dummy_disable():
        print(">>> [DUMMY] disable_proxy() executed")

    print("Testing active interface detection:")
    info = get_active_network_info()
    print("Detected:", info)

    print("\nStarting EthernetProxyWorker for 12 seconds...")
    worker = EthernetProxyWorker(dummy_enable, dummy_disable, poll_interval=3.0)
    worker.start()
    time.sleep(12)
    worker.stop()
    print("Worker stopped successfully.")
