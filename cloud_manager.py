"""
Cloud Manager for ProProxy (Supabase Integration).
High-speed user telemetry heartbeats, remote configuration, announcements, and kill switch.
Optimized for instant (< 1-2s) database updates using persistent HTTPS connection pooling.
"""

import datetime
import os
import platform
import threading
import uuid
from typing import Any, Callable, Dict, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# SUPABASE CONFIGURATION
# =============================================================================
SUPABASE_URL = "https://qdbpglyjysmfpbvfhlgh.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFkYnBnbHlqeXNtZnBidmZobGdoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc3NDI2MjgsImV4cCI6MjEwMzMxODYyOH0.SXV4dTYxAXMFbM4jG85ljhNOce6x4T86yo2t53qqv6Y"
GITHUB_REPO = "your-username/pro-proxy"  # Optional: for GitHub Releases auto-updater

# Fast timeout for instant cloud sync without UI hang
API_TIMEOUT = 3.5

# =============================================================================
# PERSISTENT CONNECTION POOLING
# Reuses TLS/TCP sockets to reduce network overhead from 1.5s+ down to ~0.3-0.8s
# =============================================================================
_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def get_http_session() -> requests.Session:
    """Returns a shared, keep-alive HTTP session with connection pooling."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
                adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retries)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


def is_cloud_configured() -> bool:
    """Checks if valid Supabase credentials are configured."""
    return (
        bool(SUPABASE_URL)
        and "YOUR_PROJECT_ID" not in SUPABASE_URL
        and bool(SUPABASE_KEY)
        and "YOUR_SUPABASE_ANON_KEY" not in SUPABASE_KEY
    )


def get_device_id() -> str:
    """
    Generates a persistent unique hardware ID for this computer.
    Uses uuid5 combined with the machine's unique hardware node.
    """
    node = uuid.getnode()
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"proproxy-device-{node}"))


def send_heartbeat(
    current_wifi: Optional[str],
    proxy_status: bool,
    app_version: str = "1.0.0"
) -> Dict[str, Any]:
    """
    Synchronously sends a fast heartbeat ping to the Supabase 'users' table.
    Upserts the record by primary key (user_id) within ~0.3 - 1.2s.
    """
    if not is_cloud_configured():
        return {"success": False, "error": "Cloud credentials not configured."}

    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/users"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"  # UPSERT by user_id
    }

    payload = {
        "user_id": get_device_id(),
        "computer_name": platform.node() or "Windows-PC",
        "app_version": app_version,
        "current_wifi": current_wifi or "Disconnected",
        "proxy_status": bool(proxy_status),
        "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

    try:
        session = get_http_session()
        response = session.post(endpoint, headers=headers, json=payload, timeout=API_TIMEOUT)
        if response.status_code in (200, 201, 204):
            return {"success": True}
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
    except Exception as error:
        return {"success": False, "error": str(error)}


def send_heartbeat_async(
    current_wifi: Optional[str],
    proxy_status: bool,
    app_version: str = "1.0.0",
    callback: Optional[Callable[[Dict[str, Any]], None]] = None
) -> threading.Thread:
    """
    Instantly dispatches a heartbeat to Supabase in a non-blocking background thread.
    Guarantees the UI thread continues immediately while Supabase is updated within ~1-2 seconds.
    """
    def _worker():
        result = send_heartbeat(current_wifi, proxy_status, app_version)
        if callback:
            try:
                callback(result)
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def fetch_remote_config() -> Optional[Dict[str, Any]]:
    """
    Fetches remote control configuration from Supabase 'remote_config' table.
    Returns config dictionary or None if unreachable.
    """
    if not is_cloud_configured():
        return None

    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/remote_config?id=eq.1"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }

    try:
        session = get_http_session()
        response = session.get(endpoint, headers=headers, timeout=API_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            elif isinstance(data, dict):
                return data
        return None
    except Exception as error:
        print(f"[CloudManager] Warning: Could not fetch remote config: {error}")
        return None
