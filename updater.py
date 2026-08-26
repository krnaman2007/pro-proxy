"""
Auto-Updater for ProProxy
Checks GitHub Releases or Cloud configuration for new versions and applies updates automatically.
"""

import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional
import requests

GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def compare_versions(v1: str, v2: str) -> int:
    """
    Compares two semantic version strings (e.g. '1.0.1' vs '1.0.0').
    Returns 1 if v1 > v2, -1 if v1 < v2, 0 if v1 == v2.
    """
    def parse(v):
        return [int(x) for x in v.lstrip("v").split(".") if x.isdigit()]

    try:
        p1, p2 = parse(v1), parse(v2)
        # Pad shorter list with zeros
        length = max(len(p1), len(p2))
        p1 += [0] * (length - len(p1))
        p2 += [0] * (length - len(p2))
        if p1 > p2:
            return 1
        elif p1 < p2:
            return -1
        return 0
    except Exception:
        return 1 if str(v1) > str(v2) else 0


def check_for_updates(
    current_version: str = "1.0.0",
    github_repo: str = ""
) -> Dict[str, Any]:
    """
    Checks for available updates on GitHub Releases.
    Returns dictionary with update details.
    """
    result: Dict[str, Any] = {
        "update_available": False,
        "latest_version": current_version,
        "download_url": "",
        "release_notes": "",
        "error": None
    }

    if not github_repo or "your-username" in github_repo:
        return result

    try:
        url = GITHUB_API_URL.format(repo=github_repo.strip("/"))
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            latest_version = data.get("tag_name", "").lstrip("v")
            result["latest_version"] = latest_version
            result["release_notes"] = data.get("body", "")

            if compare_versions(latest_version, current_version) > 0:
                # Find .exe asset
                assets = data.get("assets", [])
                for asset in assets:
                    if asset.get("name", "").lower().endswith(".exe"):
                        result["download_url"] = asset.get("browser_download_url", "")
                        result["update_available"] = True
                        break
        else:
            result["error"] = f"HTTP {response.status_code}"
    except Exception as error:
        result["error"] = str(error)

    return result


def apply_update_and_restart(download_url: str, on_progress=None) -> bool:
    """
    Downloads the new .exe and launches an updater batch script to replace
    the running binary and restart the application.
    """
    try:
        temp_dir = tempfile.gettempdir()
        new_exe_path = os.path.join(temp_dir, "ProProxy_update.exe")

        # Download new binary
        response = requests.get(download_url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(new_exe_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    file.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total_size > 0:
                        on_progress(downloaded / total_size)

        current_exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
        batch_script_path = os.path.join(temp_dir, "proproxy_updater.bat")

        # Create self-deleting batch script to replace file once process exits
        batch_content = f"""@echo off
timeout /t 2 /nobreak > nul
move /y "{new_exe_path}" "{current_exe}" > nul
start "" "{current_exe}"
del "%~f0" > nul
"""

        with open(batch_script_path, "w", encoding="utf-8") as bat_file:
            bat_file.write(batch_content)

        # Launch batch script in background
        subprocess.Popen(
            ["cmd.exe", "/c", batch_script_path],
            creationflags=CREATE_NO_WINDOW,
            close_fds=True
        )

        return True

    except Exception as error:
        print(f"[Updater] Error applying update: {error}")
        return False
