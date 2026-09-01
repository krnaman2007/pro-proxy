"""
Config Manager for Auto Proxy Switcher.
Handles loading, saving, and validating application settings in settings.json.
"""

import json
import os
import re
from typing import Any, Dict, List

# Path to settings.json in the same directory as this module
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE_PATH = os.path.join(BASE_DIR, "settings.json")

# Default configuration template
DEFAULT_CONFIG: Dict[str, Any] = {
    "proxy_ip": "172.31.100.27",
    "proxy_port": "3128",
    "enable_ethernet_proxy": True,    # Automatically enable proxy when connected via Ethernet
    "networks": ["MNNIT", "Naman_5g"],
    "check_interval": 5,
    "auto_start": True,
    "start_with_windows": False,
    "minimize_to_tray": True,
    "theme": "Dark"
}


class ConfigManager:
    """Manages application settings persistence and validation."""

    def __init__(self, config_path: str = CONFIG_FILE_PATH):
        self.config_path = config_path
        self.settings: Dict[str, Any] = self.load_settings()

    def load_settings(self) -> Dict[str, Any]:
        """
        Loads configuration from settings.json.
        If file is missing or corrupted, returns default settings and writes them.
        """
        if not os.path.exists(self.config_path):
            self.save_settings(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)

        try:
            with open(self.config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                return merged
        except (json.JSONDecodeError, OSError) as error:
            print(f"[ConfigManager] Warning: Could not read settings file ({error}). Using defaults.")
            return dict(DEFAULT_CONFIG)

    def save_settings(self, new_settings: Dict[str, Any] | None = None) -> bool:
        """
        Saves the current or provided settings to settings.json atomically.
        """
        if new_settings is not None:
            self.settings = new_settings

        temp_file = f"{self.config_path}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as file:
                json.dump(self.settings, file, indent=4)
            os.replace(temp_file, self.config_path)
            return True
        except OSError as error:
            print(f"[ConfigManager] Error: Failed to save settings ({error}).")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieves a configuration value by key."""
        return self.settings.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        """Sets a configuration value and optionally saves to disk."""
        self.settings[key] = value
        if auto_save:
            self.save_settings()

    # --- Wi-Fi SSID Helpers ---

    def get_networks(self) -> List[str]:
        """Returns the list of monitored Wi-Fi SSIDs."""
        networks = self.settings.get("networks", [])
        if isinstance(networks, list):
            return [str(net).strip() for net in networks if str(net).strip()]
        return []

    def get_monitored_networks(self) -> List[str]:
        """Alias for get_networks."""
        return self.get_networks()

    def is_network_monitored(self, ssid: str) -> bool:
        """Checks if an SSID is in the monitored network list (case-insensitive)."""
        if not ssid:
            return False
        ssid_clean = ssid.strip().lower()
        return any(net.lower() == ssid_clean for net in self.get_networks())

    def add_network(self, ssid: str) -> bool:
        """
        Adds a new Wi-Fi SSID to the monitored list if not already present.
        Returns True if added, False if duplicate or empty.
        """
        ssid = ssid.strip()
        if not ssid:
            return False

        networks = self.get_networks()
        if any(existing.lower() == ssid.lower() for existing in networks):
            return False

        networks.append(ssid)
        self.settings["networks"] = networks
        return self.save_settings()

    def remove_network(self, ssid: str) -> bool:
        """
        Removes a Wi-Fi SSID from the monitored list.
        Returns True if removed, False if not found.
        """
        ssid = ssid.strip()
        networks = self.get_networks()
        filtered = [net for net in networks if net.lower() != ssid.lower()]
        if len(filtered) == len(networks):
            return False

        self.settings["networks"] = filtered
        return self.save_settings()

    def set_networks(self, networks: List[str]) -> bool:
        """Replaces the monitored Wi-Fi list."""
        cleaned = []
        for net in networks:
            cleaned_net = str(net).strip()
            if cleaned_net and cleaned_net not in cleaned:
                cleaned.append(cleaned_net)
        self.settings["networks"] = cleaned
        return self.save_settings()

    # --- Validation Helpers ---

    @staticmethod
    def validate_ip(ip_str: str) -> bool:
        """
        Validates IP address (IPv4, IPv6) or valid hostname/domain.
        """
        ip_str = ip_str.strip()
        if not ip_str:
            return False

        ipv4_pattern = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        if re.match(ipv4_pattern, ip_str):
            return True

        hostname_pattern = r"^([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9]\.)*([A-Za-z0-9]|[A-Za-z0-9][a-zA-Z0-9\-]*[A-Za-z0-9])$"
        if re.match(hostname_pattern, ip_str):
            return True

        if ":" in ip_str and not ip_str.startswith(":") and not ip_str.endswith(":"):
            return True

        return False

    @staticmethod
    def validate_port(port_val: Any) -> bool:
        """
        Validates whether a port number is an integer in the range 1-65535.
        """
        try:
            port_num = int(str(port_val).strip())
            return 1 <= port_num <= 65535
        except (ValueError, TypeError):
            return False
