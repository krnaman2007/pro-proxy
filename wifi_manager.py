"""
Wi-Fi Interface Manager for Windows 10 and 11.
Detects active Wi-Fi interfaces, connection status, and connected SSID using `netsh wlan show interfaces`.
"""

import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

# Windows creation flag to hide console window during subprocess execution
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def get_current_wifi() -> Dict[str, Any]:
    """
    Executes 'netsh wlan show interfaces' and parses the output.
    
    Returns a dictionary with keys:
        - "connected": bool (True if Wi-Fi state is 'connected')
        - "ssid": str | None (SSID name if connected, otherwise None)
        - "state": str (e.g., 'connected', 'disconnected', 'unavailable', 'error')
        - "signal": str | None (e.g., '100%')
        - "interface_name": str | None (e.g., 'Wi-Fi')
        - "raw_output": str
        - "error": str | None
    """
    result: Dict[str, Any] = {
        "connected": False,
        "ssid": None,
        "state": "unavailable",
        "signal": None,
        "interface_name": None,
        "raw_output": "",
        "error": None
    }

    try:
        process = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=False,  # capture bytes to handle encoding safely
            creationflags=CREATE_NO_WINDOW,
            timeout=4
        )

        raw_bytes = process.stdout or process.stderr or b""
        
        # Try decoding with common Windows encodings
        decoded_text = ""
        for encoding in ["utf-8", "cp1252", "oem", "latin-1"]:
            try:
                decoded_text = raw_bytes.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue

        result["raw_output"] = decoded_text

        if process.returncode != 0:
            result["state"] = "unavailable"
            result["error"] = f"netsh returned exit code {process.returncode}"
            return result

        if not decoded_text.strip():
            result["state"] = "unavailable"
            result["error"] = "No output received from netsh."
            return result

        # Check for 'There is no wireless interface on the system.'
        if "no wireless interface" in decoded_text.lower():
            result["state"] = "no_interface"
            result["error"] = "No wireless interface detected."
            return result

        # Parse key fields using regex
        state_match = re.search(r"^\s*State\s*:\s*(.+)$", decoded_text, re.MULTILINE | re.IGNORECASE)
        ssid_match = re.search(r"^\s*SSID\s*:\s*(.+)$", decoded_text, re.MULTILINE | re.IGNORECASE)
        signal_match = re.search(r"^\s*Signal\s*:\s*(.+)$", decoded_text, re.MULTILINE | re.IGNORECASE)
        name_match = re.search(r"^\s*Name\s*:\s*(.+)$", decoded_text, re.MULTILINE | re.IGNORECASE)

        if state_match:
            state_val = state_match.group(1).strip()
            result["state"] = state_val
            result["connected"] = (state_val.lower() == "connected")
        else:
            result["state"] = "unknown"

        if result["connected"] and ssid_match:
            # Clean up SSID string
            ssid_val = ssid_match.group(1).strip()
            # If BSSID line was accidentally matched, prevent confusion
            if not ssid_val.lower().startswith("bssid"):
                result["ssid"] = ssid_val

        if signal_match:
            result["signal"] = signal_match.group(1).strip()

        if name_match:
            result["interface_name"] = name_match.group(1).strip()

        return result

    except subprocess.TimeoutExpired:
        result["state"] = "timeout"
        result["error"] = "Command 'netsh wlan show interfaces' timed out."
        return result
    except Exception as error:
        result["state"] = "error"
        result["error"] = str(error)
        return result


def is_wifi_matching(monitored_networks: list[str], current_ssid: Optional[str]) -> bool:
    """
    Checks whether the currently connected SSID matches any SSID in the monitored list.
    Case-insensitive comparison.
    """
    if not current_ssid or not monitored_networks:
        return False

    current_cleaned = current_ssid.strip().lower()
    for net in monitored_networks:
        if str(net).strip().lower() == current_cleaned:
            return True

    return False
