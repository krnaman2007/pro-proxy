"""
ProProxy - Main Application
A modern Windows 10 & 11 desktop application to automatically enable/disable
the Windows system proxy based on active network connection (Ethernet & Wi-Fi SSIDs).

Features:
- Network-Aware Proxy Switching:
  - Auto-enables proxy when connected via Ethernet (LAN).
  - Auto-enables proxy when connected to monitored Wi-Fi SSIDs.
  - Auto-disables proxy when disconnecting or connecting to unmonitored networks.
- State-Tracking Polling Worker: Prevents redundant repeated registry writes.
- Smooth Scrollable Configuration Panel: Left settings panel is fully scrollable/slideable.
- System Tray integration with minimize-to-tray & dynamic status icons.
- Windows Startup support (Run on login via HKCU Run registry).
- Cloud Telemetry (Supabase Heartbeat) & Real-time Remote Control.
- Auto-Updater with GitHub Releases integration.
- Live Network & Wi-Fi interface detection.
"""

import datetime
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import tkinter.messagebox as tkmb

import customtkinter as ctk
from PIL import Image, ImageTk
import pystray

from autostart_manager import is_autostart_enabled, set_autostart
import cloud_manager
from config import ConfigManager
import network_monitor
import proxy_manager
import updater
import wifi_manager

APP_VERSION = "1.5.0"

# Set default CustomTkinter appearance
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def get_resource_path(relative_path: str) -> str:
    """
    Resolves the absolute path to bundled resources.
    Works seamlessly in both normal Python development and PyInstaller onefile builds.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


class ProProxyApp(ctk.CTk):
    """Main Application Window for ProProxy."""

    def __init__(self):
        super().__init__()

        # --- Configuration & State ---
        self.config_mgr = ConfigManager()
        self.is_running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.is_quitting = False

        # Last detected network state for switch debouncing
        self.last_decision_state: Optional[str] = None

        # Cloud sync state
        self.last_cloud_sync = 0.0
        self.cloud_sync_interval = 6.0  # Sync with Supabase every 6 seconds

        # System Tray State
        self.tray_icon: Optional[pystray.Icon] = None
        self.tray_thread: Optional[threading.Thread] = None
        self._load_icons()

        # Network item UI tracking (ssid -> (checkbox_var, widget_frame))
        self.network_widgets: Dict[str, tuple[ctk.BooleanVar, ctk.CTkFrame]] = {}

        # --- Window Setup ---
        self.title(f"ProProxy v{APP_VERSION} - Dynamic Proxy Switcher")
        self.geometry("980x820")
        self.minsize(900, 720)

        # Set Window Icon
        self._apply_window_icon()

        # Configure responsive grid layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # Main content area expands
        self.grid_rowconfigure(4, weight=0)  # Activity log area

        # Build UI Components
        self._build_announcement_banner()
        self._build_header()
        self._build_dashboard_card()
        self._build_main_content_area()
        self._build_log_area()

        # Intercept window close (minimize to tray)
        self.protocol("WM_DELETE_WINDOW", self._on_window_close_clicked)

        # Initial UI population
        self._load_settings_into_ui()
        self._refresh_live_status()

        # Initialize and launch System Tray Icon
        self._setup_system_tray()

        # Log startup
        self.log_message(f"ProProxy v{APP_VERSION} initialized. Ready.")

        # Check command-line arguments (e.g. launched at Windows login with --minimized)
        start_minimized = "--minimized" in sys.argv or "--tray" in sys.argv
        if start_minimized:
            self.withdraw()
            self.log_message("Started minimized to system tray.")
        else:
            self.deiconify()
            self.lift()
            self.focus_force()

        # Trigger background Cloud & Update check
        threading.Thread(target=self._initial_cloud_check, daemon=True).start()

        # Auto-start proxy service if enabled in settings
        if self.config_mgr.get("auto_start", True):
            self.start_service()

    # =========================================================================
    # ICONS & WINDOW SETUP
    # =========================================================================

    def _load_icons(self):
        """Loads or prepares PIL image icons for the window and system tray."""
        self.icon_ico_path = get_resource_path("icon.ico")
        self.icon_png_path = get_resource_path("icon.png")
        self.tray_on_path = get_resource_path("tray_on.png")
        self.tray_off_path = get_resource_path("tray_off.png")

        try:
            self.img_app = Image.open(self.icon_png_path)
        except Exception:
            self.img_app = Image.new("RGBA", (64, 64), color="#1E293B")

        try:
            self.img_tray_on = Image.open(self.tray_on_path)
        except Exception:
            self.img_tray_on = self.img_app

        try:
            self.img_tray_off = Image.open(self.tray_off_path)
        except Exception:
            self.img_tray_off = self.img_app

    def _apply_window_icon(self):
        """Sets the application icon on the Tkinter titlebar and taskbar."""
        try:
            if os.path.exists(self.icon_ico_path):
                self.iconbitmap(self.icon_ico_path)
            elif os.path.exists(self.icon_png_path):
                icon_img = ImageTk.PhotoImage(file=self.icon_png_path)
                self.iconphoto(False, icon_img)
        except Exception as error:
            print(f"[ProProxy] Notice: Could not set window icon: {error}")

    # =========================================================================
    # CLOUD SYNC, KILL SWITCH & AUTO-UPDATER
    # =========================================================================

    def _initial_cloud_check(self):
        """Runs in background on startup to check kill switch, announcements, and telemetry."""
        # 1. Fetch Remote Config
        remote_cfg = cloud_manager.fetch_remote_config()
        if remote_cfg:
            if not remote_cfg.get("app_enabled", True):
                kill_msg = remote_cfg.get("kill_message", "This application is currently deactivated.")
                self.after(0, lambda: self._trigger_kill_switch(kill_msg))
                return

            announcement = remote_cfg.get("announcement", "").strip()
            if announcement:
                self.after(0, lambda: self._show_announcement(announcement))

            remote_ip = remote_cfg.get("default_proxy_ip")
            remote_port = remote_cfg.get("default_proxy_port")
            if remote_ip and remote_port:
                current_ip = self.config_mgr.get("proxy_ip")
                if current_ip in ("127.0.0.1", ""):
                    self.config_mgr.set("proxy_ip", remote_ip, auto_save=False)
                    self.config_mgr.set("proxy_port", str(remote_port), auto_save=True)
                    self.after(0, self._load_settings_into_ui)

        # 2. Send initial telemetry heartbeat
        net_info = network_monitor.get_active_network_info()
        current_net = "Ethernet" if net_info.get("is_ethernet") else net_info.get("ssid")
        proxy_info = proxy_manager.get_current_proxy_status()
        cloud_manager.send_heartbeat(
            current_wifi=current_net,
            proxy_status=proxy_info.get("enabled", False),
            app_version=APP_VERSION
        )

    def _trigger_kill_switch(self, message: str):
        """Displays kill switch notice and terminates the application."""
        self.deiconify()
        self.lift()
        tkmb.showerror(
            "ProProxy - Service Notice",
            f"⚠️ Application Disabled\n\n{message}\n\nProProxy will now close."
        )
        self.quit_application()

    def _show_announcement(self, message: str):
        """Displays an announcement dialog."""
        self.deiconify()
        self.lift()
        tkmb.showinfo("ProProxy - Announcement", f"📢 Announcement:\n\n{message}")

    def _prompt_update(self, update_info: Dict[str, Any]):
        """Prompts user to install a new version."""
        latest_ver = update_info.get("latest_version", "New")
        notes = update_info.get("release_notes", "")
        download_url = update_info.get("download_url", "")

        msg = f"A new version of ProProxy (v{latest_ver}) is available!\n"
        if notes:
            msg += f"\nRelease notes:\n{notes[:200]}\n"
        msg += "\nWould you like to download and install this update automatically?"

        if tkmb.askyesno("Update Available", msg):
            self.log_message(f"⬇️ Downloading update v{latest_ver}...")
            threading.Thread(
                target=lambda: updater.apply_update_and_restart(download_url),
                daemon=True
            ).start()

    def check_updates_manual(self):
        """Manually triggered update check."""
        self.log_message("🔍 Checking for updates...")
        def _check():
            info = updater.check_for_updates(
                current_version=APP_VERSION,
                github_repo=cloud_manager.GITHUB_REPO
            )
            if info.get("update_available"):
                self.after(0, lambda: self._prompt_update(info))
            else:
                self.after(0, lambda: tkmb.showinfo(
                    "ProProxy Updates",
                    f"You are running the latest version of ProProxy (v{APP_VERSION})."
                ))
        threading.Thread(target=_check, daemon=True).start()

    # =========================================================================
    # SYSTEM TRAY INTEGRATION (pystray)
    # =========================================================================

    def _setup_system_tray(self):
        """Initializes the background system tray icon with interactive menu."""
        menu = pystray.Menu(
            pystray.MenuItem("⚡ Open ProProxy", self._tray_show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda text: self._get_tray_proxy_status_text(), None, enabled=False),
            pystray.MenuItem(lambda text: self._get_tray_service_status_text(), self._tray_toggle_service),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Exit Application", self._tray_exit_app)
        )

        self.tray_icon = pystray.Icon(
            "ProProxy",
            self.img_tray_off,
            f"ProProxy v{APP_VERSION}",
            menu
        )

        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def _get_tray_proxy_status_text(self) -> str:
        """Returns dynamic menu text for proxy status."""
        proxy_info = proxy_manager.get_current_proxy_status()
        if proxy_info.get("enabled"):
            server = proxy_info.get("server") or "Active"
            return f"System Proxy: ON ({server})"
        return "System Proxy: OFF"

    def _get_tray_service_status_text(self) -> str:
        """Returns dynamic menu text for service state."""
        if self.is_running:
            return "⏹ Stop Monitoring Service"
        return "▶ Start Monitoring Service"

    def _update_tray_icon(self, proxy_enabled: bool):
        """Updates the system tray icon image to reflect Proxy ON/OFF state."""
        if not self.tray_icon:
            return
        try:
            target_image = self.img_tray_on if proxy_enabled else self.img_tray_off
            self.tray_icon.icon = target_image
            status_text = "System Proxy: ON" if proxy_enabled else "System Proxy: OFF"
            service_text = "Running" if self.is_running else "Stopped"
            self.tray_icon.title = f"ProProxy [{status_text} | {service_text}]"
        except Exception:
            pass

    def _tray_show_window(self, icon=None, item=None):
        """Restores and focuses the main window from the system tray."""
        self.after(0, self._restore_window)

    def _restore_window(self):
        """Restores window from minimized state."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_toggle_service(self, icon=None, item=None):
        """Toggles the service from system tray menu."""
        self.after(0, self.toggle_service)

    def _tray_exit_app(self, icon=None, item=None):
        """Exits the application from the tray menu."""
        self.after(0, self.quit_application)

    def _on_window_close_clicked(self):
        """Intercepts window close button to minimize to tray instead of quitting."""
        if self.config_mgr.get("minimize_to_tray", True):
            self.withdraw()
            self.log_message("ProProxy minimized to System Tray.")
        else:
            self.quit_application()

    def quit_application(self):
        """Completely shuts down service, tray icon, and destroys window."""
        self.is_quitting = True
        self.stop_service()
        proxy_manager.disable_proxy()

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        self.destroy()
        sys.exit(0)

    # =========================================================================
    # UI BUILDERS
    # =========================================================================

    def _build_announcement_banner(self):
        """Builds top notice/announcement area."""
        self.banner_frame = ctk.CTkFrame(self, fg_color="#3B82F6", corner_radius=0, height=0)
        self.banner_frame.grid(row=0, column=0, sticky="ew")

    def _build_header(self):
        """Builds modern header with title, version, status badge, and theme controls."""
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=1, column=0, padx=20, pady=(15, 5), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)

        # Title & Subtitle box
        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")

        title_lbl = ctk.CTkLabel(
            title_box,
            text=f"⚡ ProProxy ",
            font=ctk.CTkFont(size=22, weight="bold")
        )
        title_lbl.pack(anchor="w")

        sub_lbl = ctk.CTkLabel(
            title_box,
            text=" Auto Proxy Switcher (Ethernet & Wi-Fi)",
            font=ctk.CTkFont(size=12),
            text_color="gray60"
        )
        sub_lbl.pack(anchor="w")

        # Right side controls
        controls_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        controls_box.grid(row=0, column=1, sticky="e")

        self.service_badge = ctk.CTkLabel(
            controls_box,
            text="● SERVICE STOPPED",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#E5E7EB", "#374151"),
            text_color=("#6B7280", "#9CA3AF"),
            corner_radius=8,
            padx=12,
            pady=4
        )
        self.service_badge.pack(side="left", padx=(0, 10))

        btn_min_tray = ctk.CTkButton(
            controls_box,
            text="📥 Hide to Tray",
            font=ctk.CTkFont(size=11),
            width=95,
            height=28,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self._on_window_close_clicked
        )
        btn_min_tray.pack(side="left", padx=(0, 8))

        self.theme_switch = ctk.CTkOptionMenu(
            controls_box,
            values=["Dark", "Light", "System"],
            command=self._on_theme_change,
            width=90,
            height=28
        )
        self.theme_switch.set(self.config_mgr.get("theme", "Dark"))
        self.theme_switch.pack(side="left")

    def _build_dashboard_card(self):
        """Builds real-time summary cards with vibrant status indicators."""
        dash_frame = ctk.CTkFrame(self, corner_radius=12)
        dash_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
        dash_frame.grid_columnconfigure((0, 1, 2), weight=1)

        # Card 1: Current Connection (Ethernet or Wi-Fi)
        conn_card = ctk.CTkFrame(dash_frame, fg_color=("gray90", "gray17"), corner_radius=10)
        conn_card.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")

        ctk.CTkLabel(
            conn_card,
            text="🌐 ACTIVE CONNECTION",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray60"
        ).pack(anchor="w", padx=12, pady=(10, 2))

        self.dash_conn_name = ctk.CTkLabel(
            conn_card,
            text="Detecting...",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=("#2563EB", "#60A5FA")
        )
        self.dash_conn_name.pack(anchor="w", padx=12, pady=(0, 2))

        self.dash_conn_detail = ctk.CTkLabel(
            conn_card,
            text="Checking network adapter...",
            font=ctk.CTkFont(size=11),
            text_color="gray60"
        )
        self.dash_conn_detail.pack(anchor="w", padx=12, pady=(0, 10))

        # Card 2: System Proxy Status
        proxy_card = ctk.CTkFrame(dash_frame, fg_color=("gray90", "gray17"), corner_radius=10)
        proxy_card.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")

        ctk.CTkLabel(
            proxy_card,
            text="🌐 STATUS",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray60"
        ).pack(anchor="w", padx=12, pady=(10, 2))

        self.dash_proxy_status = ctk.CTkLabel(
            proxy_card,
            text="Checking...",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=("#10B981", "#34D399")
        )
        self.dash_proxy_status.pack(anchor="w", padx=12, pady=(0, 2))

        self.dash_proxy_server = ctk.CTkLabel(
            proxy_card,
            text="Server: -",
            font=ctk.CTkFont(size=11),
            text_color="gray60"
        )
        self.dash_proxy_server.pack(anchor="w", padx=12, pady=(0, 10))

        # Card 3: Switch Decision
        switch_card = ctk.CTkFrame(dash_frame, fg_color=("gray90", "gray17"), corner_radius=10)
        switch_card.grid(row=0, column=2, padx=8, pady=8, sticky="nsew")

        ctk.CTkLabel(
            switch_card,
            text="⚙️ Current Network ",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray60"
        ).pack(anchor="w", padx=12, pady=(10, 2))

        self.dash_match_status = ctk.CTkLabel(
            switch_card,
            text="Idle (Service Stopped)",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="gray70"
        )
        self.dash_match_status.pack(anchor="w", padx=12, pady=(0, 2))

        self.dash_match_detail = ctk.CTkLabel(
            switch_card,
            text="Auto-check every 5 seconds",
            font=ctk.CTkFont(size=11),
            text_color="gray60"
        )
        self.dash_match_detail.pack(anchor="w", padx=12, pady=(0, 10))

    def _build_main_content_area(self):
        """
        Builds two-column area:
        Left Column: Fully SCROLLABLE / SLIDING Proxy Configuration & Controller Panel.
        Right Column: Monitored Wi-Fi Networks list.
        """
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.grid(row=3, column=0, padx=20, pady=5, sticky="nsew")
        content_frame.grid_columnconfigure(0, weight=4)  # Left column (scrollable)
        content_frame.grid_columnconfigure(1, weight=5)  # Right column
        content_frame.grid_rowconfigure(0, weight=1)

        # ---------------------------------------------------------------------
        # LEFT PANEL: Fully Scrollable / Sliding Proxy Configuration Panel
        # ---------------------------------------------------------------------
        self.left_scroll_panel = ctk.CTkScrollableFrame(
            content_frame,
            corner_radius=12,
            fg_color=("gray90", "gray17")
        )
        self.left_scroll_panel.grid(row=0, column=0, padx=(0, 10), pady=0, sticky="nsew")
        self.left_scroll_panel.grid_columnconfigure(0, weight=1)

        # Panel Title
        ctk.CTkLabel(
            self.left_scroll_panel,
            text="🔧 Proxy Configuration",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Proxy Address Input
        ctk.CTkLabel(
            self.left_scroll_panel,
            text="Proxy Address:",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=12, pady=(4, 2))

        self.entry_ip = ctk.CTkEntry(
            self.left_scroll_panel,
            placeholder_text="e.g. 172.31.100.27 or proxy.example.com",
            height=32
        )
        self.entry_ip.pack(fill="x", padx=12, pady=(0, 6))
        self.entry_ip.bind("<KeyRelease>", lambda e: self._on_settings_modified())

        # Proxy Port Input
        ctk.CTkLabel(
            self.left_scroll_panel,
            text="Proxy Port:",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=12, pady=(2, 2))

        self.entry_port = ctk.CTkEntry(
            self.left_scroll_panel,
            placeholder_text="e.g. 3128 or 8080",
            height=32
        )
        self.entry_port.pack(fill="x", padx=12, pady=(0, 8))
        self.entry_port.bind("<KeyRelease>", lambda e: self._on_settings_modified())

        # Checkboxes: Network & Application Options
        options_box = ctk.CTkFrame(self.left_scroll_panel, fg_color="transparent")
        options_box.pack(fill="x", padx=12, pady=(0, 6))

        self.chk_ethernet_proxy = ctk.CTkCheckBox(
            options_box,
            text="🔌 Auto-enable proxy when on Ethernet (LAN)",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._on_settings_modified
        )
        self.chk_ethernet_proxy.pack(anchor="w", pady=(2, 4))

        self.chk_autostart = ctk.CTkCheckBox(
            options_box,
            text="Turn on automatically when app opens",
            font=ctk.CTkFont(size=12),
            command=self._on_settings_modified
        )
        self.chk_autostart.pack(anchor="w", pady=(1, 3))

        self.chk_windows_startup = ctk.CTkCheckBox(
            options_box,
            text="Start app when computer turns on",
            font=ctk.CTkFont(size=12),
            command=self._on_windows_startup_toggled
        )
        self.chk_windows_startup.pack(anchor="w", pady=(1, 3))

        self.chk_minimize_tray = ctk.CTkCheckBox(
            options_box,
            text="Keep running in background when closed",
            font=ctk.CTkFont(size=12),
            command=self._on_settings_modified
        )
        self.chk_minimize_tray.pack(anchor="w", pady=(1, 3))

        # Save & Refresh Button Row
        save_box = ctk.CTkFrame(self.left_scroll_panel, fg_color="transparent")
        save_box.pack(fill="x", padx=12, pady=(4, 8))
        save_box.grid_columnconfigure((0, 1), weight=1)

        self.btn_save = ctk.CTkButton(
            save_box,
            text="💾 Save Settings",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#3B82F6", "#2563EB"),
            hover_color=("#2563EB", "#1D4ED8"),
            height=30,
            command=self.save_settings
        )
        self.btn_save.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_refresh = ctk.CTkButton(
            save_box,
            text="🔄 Refresh Status",
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=30,
            command=self._refresh_live_status
        )
        self.btn_refresh.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # Separator Line
        separator = ctk.CTkFrame(self.left_scroll_panel, height=2, fg_color=("gray80", "gray25"))
        separator.pack(fill="x", padx=12, pady=6)

        # Service Control Section
        ctk.CTkLabel(
            self.left_scroll_panel,
            text="🚀 Service Controller",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(anchor="w", padx=12, pady=(4, 6))

        self.btn_toggle_service = ctk.CTkButton(
            self.left_scroll_panel,
            text="▶ START PROXY SERVICE",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("#10B981", "#059669"),
            hover_color=("#059669", "#047857"),
            height=38,
            corner_radius=8,
            command=self.toggle_service
        )
        self.btn_toggle_service.pack(fill="x", padx=12, pady=(0, 6))

        # Manual Testing Controls
        manual_box = ctk.CTkFrame(self.left_scroll_panel, fg_color="transparent")
        manual_box.pack(fill="x", padx=12, pady=(0, 6))
        manual_box.grid_columnconfigure((0, 1), weight=1)

        self.btn_manual_on = ctk.CTkButton(
            manual_box,
            text="Manual Proxy ON",
            font=ctk.CTkFont(size=11),
            fg_color=("gray70", "gray25"),
            hover_color=("gray60", "gray35"),
            height=28,
            command=self._manual_enable_proxy
        )
        self.btn_manual_on.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_manual_off = ctk.CTkButton(
            manual_box,
            text="Manual Proxy OFF",
            font=ctk.CTkFont(size=11),
            fg_color=("gray70", "gray25"),
            hover_color=("gray60", "gray35"),
            height=28,
            command=self._manual_disable_proxy
        )
        self.btn_manual_off.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # Check for Updates Button
        self.btn_check_update = ctk.CTkButton(
            self.left_scroll_panel,
            text="🔍 Check for Updates",
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            hover_color=("gray85", "gray25"),
            text_color=("gray40", "gray60"),
            height=24,
            command=self.check_updates_manual
        )
        self.btn_check_update.pack(fill="x", padx=12, pady=(4, 12))

        # ---------------------------------------------------------------------
        # RIGHT PANEL: Monitored Wi-Fi Networks
        # ---------------------------------------------------------------------
        right_panel = ctk.CTkFrame(content_frame, corner_radius=12)
        right_panel.grid(row=0, column=1, padx=(10, 0), pady=0, sticky="nsew")
        right_panel.grid_columnconfigure(0, weight=1)
        right_panel.grid_rowconfigure(3, weight=1)  # Network list expands

        # Panel Title
        ctk.CTkLabel(
            right_panel,
            text="📡 Added Proxy Networks",
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, padx=16, pady=(14, 4), sticky="w")

        ctk.CTkLabel(
            right_panel,
            text="Proxy will be ENABLED when connected to any network in this list:",
            font=ctk.CTkFont(size=11),
            text_color="gray60"
        ).grid(row=1, column=0, padx=16, pady=(0, 8), sticky="w")

        # Add Network Input Row
        add_box = ctk.CTkFrame(right_panel, fg_color="transparent")
        add_box.grid(row=2, column=0, padx=16, pady=(0, 8), sticky="ew")
        add_box.grid_columnconfigure(0, weight=1)

        self.entry_new_ssid = ctk.CTkEntry(
            add_box,
            placeholder_text="Enter Wi-Fi Name (e.g. MNNIT or Naman_5g)",
            height=34
        )
        self.entry_new_ssid.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.entry_new_ssid.bind("<Return>", lambda e: self.add_network_from_entry())

        self.btn_add_network = ctk.CTkButton(
            add_box,
            text="➕ Add Network",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#3B82F6", "#2563EB"),
            hover_color=("#2563EB", "#1D4ED8"),
            width=110,
            height=34,
            command=self.add_network_from_entry
        )
        self.btn_add_network.grid(row=0, column=1, sticky="e")

        # Scrollable Network List Container
        self.networks_scroll_frame = ctk.CTkScrollableFrame(
            right_panel,
            corner_radius=8,
            fg_color=("gray90", "gray17")
        )
        self.networks_scroll_frame.grid(row=3, column=0, padx=16, pady=(0, 8), sticky="nsew")
        self.networks_scroll_frame.grid_columnconfigure(0, weight=1)

        # Batch Operations Box (Scan / Select All / Delete All)
        batch_box = ctk.CTkFrame(right_panel, fg_color="transparent")
        batch_box.grid(row=4, column=0, padx=16, pady=(0, 14), sticky="ew")
        batch_box.grid_columnconfigure((0, 1, 2), weight=1)

        self.btn_scan_wifi = ctk.CTkButton(
            batch_box,
            text="📡 Scan Available",
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=28,
            command=self.scan_and_show_available_wifi
        )
        self.btn_scan_wifi.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_add_current = ctk.CTkButton(
            batch_box,
            text="➕ Add Current Wi-Fi",
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=28,
            command=self.add_current_connected_wifi
        )
        self.btn_add_current.grid(row=0, column=1, padx=4, sticky="ew")

        self.btn_clear_networks = ctk.CTkButton(
            batch_box,
            text="🗑️ Clear All",
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            hover_color=("#EF4444", "#DC2626"),
            height=28,
            command=self.clear_all_networks
        )
        self.btn_clear_networks.grid(row=0, column=2, padx=(4, 0), sticky="ew")

    def _build_log_area(self):
        """Builds the bottom activity log area with timestamped events."""
        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.grid(row=4, column=0, padx=20, pady=(5, 15), sticky="ew")
        log_frame.grid_columnconfigure(0, weight=1)

        # Header with Clear button
        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, padx=16, pady=(10, 4), sticky="ew")
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header,
            text="📋 Activity Log",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        btn_clear = ctk.CTkButton(
            log_header,
            text="Clear Log",
            font=ctk.CTkFont(size=11),
            width=70,
            height=22,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self.clear_log
        )
        btn_clear.grid(row=0, column=1, sticky="e")

        # Textbox for logs
        self.log_textbox = ctk.CTkTextbox(
            log_frame,
            height=95,
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=8
        )
        self.log_textbox.grid(row=1, column=0, padx=16, pady=(0, 12), sticky="ew")
        self.log_textbox.configure(state="disabled")

    # =========================================================================
    # SETTINGS & DATA BINDING
    # =========================================================================

    def _load_settings_into_ui(self):
        """Loads configuration from settings.json into the UI widgets."""
        saved_ip = self.config_mgr.get("proxy_ip", "172.31.100.27")
        saved_port = str(self.config_mgr.get("proxy_port", "3128"))

        self.entry_ip.delete(0, "end")
        self.entry_ip.insert(0, saved_ip)

        self.entry_port.delete(0, "end")
        self.entry_port.insert(0, saved_port)

        # Checkboxes
        self.chk_ethernet_proxy.select() if self.config_mgr.get("enable_ethernet_proxy", True) else self.chk_ethernet_proxy.deselect()
        self.chk_autostart.select() if self.config_mgr.get("auto_start", True) else self.chk_autostart.deselect()
        self.chk_minimize_tray.select() if self.config_mgr.get("minimize_to_tray", True) else self.chk_minimize_tray.deselect()

        # Windows Startup registry state
        windows_startup_actual = is_autostart_enabled()
        self.chk_windows_startup.select() if windows_startup_actual else self.chk_windows_startup.deselect()
        self.config_mgr.set("start_with_windows", windows_startup_actual, auto_save=True)

        # Refresh network list items
        self._refresh_network_list_ui()

    def _sync_ui_to_config(self, auto_save: bool = True):
        """Pulls values from UI widgets and updates the ConfigManager instance."""
        ip = self.entry_ip.get().strip() or "172.31.100.27"
        port = self.entry_port.get().strip() or "3128"

        self.config_mgr.set("proxy_ip", ip, auto_save=False)
        self.config_mgr.set("proxy_port", port, auto_save=False)
        self.config_mgr.set("enable_ethernet_proxy", bool(self.chk_ethernet_proxy.get()), auto_save=False)
        self.config_mgr.set("auto_start", bool(self.chk_autostart.get()), auto_save=False)
        self.config_mgr.set("minimize_to_tray", bool(self.chk_minimize_tray.get()), auto_save=False)
        self.config_mgr.set("theme", self.theme_switch.get(), auto_save=auto_save)

    def _on_settings_modified(self):
        """Called whenever an entry or checkbox is changed in the UI."""
        self._sync_ui_to_config(auto_save=True)

    def _on_windows_startup_toggled(self):
        """Handles user clicking the 'Start with Windows' checkbox."""
        enable = bool(self.chk_windows_startup.get())
        success, message = set_autostart(enable, minimized=True)
        if success:
            self.config_mgr.set("start_with_windows", enable, auto_save=True)
            status = "enabled" if enable else "disabled"
            self.log_message(f"⚙️ Windows Startup {status} successfully.")
        else:
            self.log_message(f"❌ Failed to modify Windows Startup: {message}", "error")
            self.chk_windows_startup.select() if not enable else self.chk_windows_startup.deselect()

    def _on_theme_change(self, new_theme: str):
        """Switches CustomTkinter appearance mode dynamically."""
        ctk.set_appearance_mode(new_theme)
        self.config_mgr.set("theme", new_theme, auto_save=True)
        self.log_message(f"🎨 Theme changed to: {new_theme}")

    def save_settings(self):
        """Validates and manually saves settings."""
        ip = self.entry_ip.get().strip()
        port = self.entry_port.get().strip()

        if not ConfigManager.validate_ip(ip):
            tkmb.showerror("Invalid Setting", f"'{ip}' is not a valid IPv4, IPv6 address, or hostname.")
            return

        if not ConfigManager.validate_port(port):
            tkmb.showerror("Invalid Setting", f"'{port}' is not a valid port number (must be 1-65535).")
            return

        self._sync_ui_to_config(auto_save=True)
        self.log_message(f"💾 Settings saved successfully: {ip}:{port}")
        self._refresh_live_status()

    # =========================================================================
    # NETWORK LIST MANAGEMENT
    # =========================================================================

    def _refresh_network_list_ui(self):
        """Renders the list of monitored Wi-Fi SSIDs in the scrollable frame."""
        for widget in self.networks_scroll_frame.winfo_children():
            widget.destroy()
        self.network_widgets.clear()

        networks = self.config_mgr.get_networks()
        wifi_info = wifi_manager.get_current_wifi()
        current_ssid = wifi_info.get("ssid") if wifi_info.get("connected") else None

        if not networks:
            empty_lbl = ctk.CTkLabel(
                self.networks_scroll_frame,
                text="No monitored networks added.\nEnter an SSID above and click 'Add Network'.",
                font=ctk.CTkFont(size=12),
                text_color="gray50"
            )
            empty_lbl.pack(pady=30)
            return

        for index, ssid in enumerate(networks):
            is_active_network = (current_ssid and current_ssid.lower() == ssid.lower())

            item_frame = ctk.CTkFrame(
                self.networks_scroll_frame,
                fg_color=("#E5E7EB", "#262B36") if is_active_network else ("gray85", "gray22"),
                corner_radius=8
            )
            item_frame.pack(fill="x", padx=4, pady=3)
            item_frame.grid_columnconfigure(1, weight=1)

            chk_var = ctk.BooleanVar(value=True)
            chk = ctk.CTkCheckBox(
                item_frame,
                text="",
                variable=chk_var,
                width=24,
                command=lambda s=ssid, v=chk_var: self._on_network_item_toggled(s, v)
            )
            chk.grid(row=0, column=0, padx=(10, 5), pady=8)

            ssid_lbl = ctk.CTkLabel(
                item_frame,
                text=ssid,
                font=ctk.CTkFont(size=13, weight="bold" if is_active_network else "normal"),
                anchor="w"
            )
            ssid_lbl.grid(row=0, column=1, sticky="w", padx=4)

            if is_active_network:
                badge = ctk.CTkLabel(
                    item_frame,
                    text="● CONNECTED",
                    font=ctk.CTkFont(size=10, weight="bold"),
                    fg_color=("#DCFCE7", "#064E3B"),
                    text_color=("#16A34A", "#34D399"),
                    corner_radius=6,
                    padx=6,
                    pady=1
                )
                badge.grid(row=0, column=2, padx=6)

            btn_remove = ctk.CTkButton(
                item_frame,
                text="🗑️",
                width=30,
                height=26,
                fg_color="transparent",
                hover_color=("#FEE2E2", "#7F1D1D"),
                text_color=("#EF4444", "#F87171"),
                command=lambda s=ssid: self.remove_network(s)
            )
            btn_remove.grid(row=0, column=3, padx=(4, 8))

            self.network_widgets[ssid] = (chk_var, item_frame)

    def _on_network_item_toggled(self, ssid: str, var: ctk.BooleanVar):
        """Handles unchecking a network item."""
        if not var.get():
            self.remove_network(ssid)

    def add_network_from_entry(self):
        """Adds SSID from the text entry to the monitored list."""
        ssid = self.entry_new_ssid.get().strip()
        if not ssid:
            return

        if self.config_mgr.add_network(ssid):
            self.entry_new_ssid.delete(0, "end")
            self.log_message(f"➕ Added monitored network: '{ssid}'")
            self._refresh_network_list_ui()
            if self.is_running:
                self._check_and_switch_proxy()
        else:
            self.log_message(f"⚠️ Network '{ssid}' is already in the list.")

    def add_current_connected_wifi(self):
        """Detects current connected Wi-Fi and adds it to the list."""
        wifi_info = wifi_manager.get_current_wifi()
        if wifi_info.get("connected") and wifi_info.get("ssid"):
            ssid = wifi_info["ssid"]
            if self.config_mgr.add_network(ssid):
                self.log_message(f"➕ Added currently connected Wi-Fi: '{ssid}'")
                self._refresh_network_list_ui()
                if self.is_running:
                    self._check_and_switch_proxy()
            else:
                self.log_message(f"ℹ️ Current Wi-Fi '{ssid}' is already monitored.")
        else:
            self.log_message("⚠️ No active Wi-Fi connection detected.", "warn")

    def remove_network(self, ssid: str):
        """Removes an SSID from the monitored list."""
        if self.config_mgr.remove_network(ssid):
            self.log_message(f"🗑️ Removed network: '{ssid}'")
            self._refresh_network_list_ui()
            if self.is_running:
                self._check_and_switch_proxy()

    def scan_and_show_available_wifi(self):
        """Scans for nearby wireless SSIDs and displays a selection dialog."""
        self.log_message("📡 Scanning for available Wi-Fi networks...")
        available = wifi_manager.scan_available_wifis()

        if not available:
            self.log_message("ℹ️ No available Wi-Fi networks found.")
            tkmb.showinfo("Wi-Fi Scan", "No visible Wi-Fi networks detected in range.")
            return

        scan_win = ctk.CTkToplevel(self)
        scan_win.title("Available Wi-Fi Networks")
        scan_win.geometry("380x420")
        scan_win.grab_set()

        ctk.CTkLabel(
            scan_win,
            text="Select networks to monitor:",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(padx=16, pady=(14, 8), anchor="w")

        scroll = ctk.CTkScrollableFrame(scan_win)
        scroll.pack(fill="both", expand=True, padx=16, pady=4)

        monitored = self.config_mgr.get_networks()
        check_vars: Dict[str, ctk.BooleanVar] = {}

        for net in available:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)

            is_already = any(m.lower() == net["ssid"].lower() for m in monitored)
            var = ctk.BooleanVar(value=is_already)
            check_vars[net["ssid"]] = var

            chk = ctk.CTkCheckBox(row, text=f"{net['ssid']} ({net.get('signal', 'N/A')})", variable=var)
            chk.pack(side="left", padx=4)

        def _apply():
            for ssid, var in check_vars.items():
                if var.get():
                    self.config_mgr.add_network(ssid)
                else:
                    self.config_mgr.remove_network(ssid)
            self._refresh_network_list_ui()
            self.log_message("✅ Applied network selections from scan.")
            scan_win.destroy()
            if self.is_running:
                self._check_and_switch_proxy()

        ctk.CTkButton(scan_win, text="Apply Selection", command=_apply, height=34).pack(fill="x", padx=16, pady=12)

    def clear_all_networks(self):
        """Clears all monitored networks."""
        self.config_mgr.set_networks([])
        self.log_message("🗑️ Cleared all monitored Wi-Fi networks.")
        self._refresh_network_list_ui()
        if self.is_running:
            self._check_and_switch_proxy()

    # =========================================================================
    # LIVE STATUS & PROXY ACTIONS
    # =========================================================================

    def _refresh_live_status(self):
        """Refreshes the live dashboard widgets and tray icon."""
        # 1. Query Active Network (Ethernet / Wi-Fi / Disconnected)
        net_info = network_monitor.get_active_network_info()
        if net_info.get("is_ethernet"):
            iface_name = net_info.get("interface_name", "Ethernet")
            self.dash_conn_name.configure(
                text="🔌 Ethernet (LAN)",
                text_color=("#10B981", "#34D399")
            )
            self.dash_conn_detail.configure(
                text=f"Adapter: {iface_name} | IP: {net_info.get('ip_address', 'N/A')}"
            )
        elif net_info.get("type") == "wifi" and net_info.get("ssid"):
            ssid = net_info["ssid"]
            self.dash_conn_name.configure(
                text=f"📶 {ssid}",
                text_color=("#2563EB", "#60A5FA")
            )
            self.dash_conn_detail.configure(
                text=f"Wi-Fi Connected | IP: {net_info.get('ip_address', 'N/A')}"
            )
        elif net_info.get("type") == "disconnected":
            self.dash_conn_name.configure(
                text="❌ Disconnected",
                text_color="gray60"
            )
            self.dash_conn_detail.configure(text="No active network connection detected")
        else:
            iface = net_info.get("interface_name", "Network")
            self.dash_conn_name.configure(
                text=f"🌐 {iface}",
                text_color=("#2563EB", "#60A5FA")
            )
            self.dash_conn_detail.configure(
                text=f"IP: {net_info.get('ip_address', 'N/A')}"
            )

        # 2. Query System Proxy Status
        proxy_info = proxy_manager.get_current_proxy_status()
        is_proxy_enabled = proxy_info.get("enabled", False)

        if is_proxy_enabled:
            server = proxy_info.get("server") or "Active"
            self.dash_proxy_status.configure(
                text="🟢 SYSTEM PROXY ON",
                text_color=("#10B981", "#34D399")
            )
            self.dash_proxy_server.configure(text=f"Active Server: {server}")
        else:
            self.dash_proxy_status.configure(
                text="🔴 SYSTEM PROXY OFF",
                text_color=("#EF4444", "#F87171")
            )
            server = proxy_info.get("server")
            self.dash_proxy_server.configure(
                text=f"Last Server: {server}" if server else "Server: None"
            )

        # Update system tray icon
        self._update_tray_icon(is_proxy_enabled)

    def _manual_enable_proxy(self):
        """Manually enables the Windows system proxy."""
        ip = self.entry_ip.get().strip()
        port = self.entry_port.get().strip()
        if not ConfigManager.validate_port(port):
            self.log_message(f"❌ Invalid port: {port}", "error")
            return

        success, msg = proxy_manager.enable_proxy(ip, port)
        if success:
            self.log_message(f"🌐 {msg}")
        else:
            self.log_message(f"❌ {msg}", "error")

        self._refresh_live_status()

    def _manual_disable_proxy(self):
        """Manually disables the Windows system proxy."""
        success, msg = proxy_manager.disable_proxy()
        if success:
            self.log_message(f"🌐 {msg}")
        else:
            self.log_message(f"❌ {msg}", "error")

        self._refresh_live_status()

    # =========================================================================
    # BACKGROUND SERVICE & MONITORING LOOP (Network-Aware Polling)
    # =========================================================================

    def toggle_service(self):
        """Toggles background proxy switching service on or off."""
        if self.is_running:
            self.stop_service()
        else:
            self.start_service()

    def start_service(self):
        """Starts the background monitoring thread."""
        if self.is_running:
            return

        port = self.entry_port.get().strip()
        if not ConfigManager.validate_port(port):
            self.log_message(f"❌ Cannot start service: Invalid port '{port}'.", "error")
            return

        self._sync_ui_to_config(auto_save=True)
        self.is_running = True
        self.stop_event.clear()
        self.last_decision_state = None  # Reset debouncing

        self.btn_toggle_service.configure(
            text="⏹ STOP PROXY SERVICE",
            fg_color=("#EF4444", "#DC2626"),
            hover_color=("#DC2626", "#B91C1C")
        )
        self.service_badge.configure(
            text="● SERVICE ACTIVE",
            fg_color=("#DCFCE7", "#064E3B"),
            text_color=("#16A34A", "#34D399")
        )
        self.dash_match_status.configure(
            text="Monitoring Active",
            text_color=("#10B981", "#34D399")
        )

        self.log_message("▶ ProProxy service STARTED. Polling network adapters every 5s...")

        self.monitor_thread = threading.Thread(target=self._monitor_worker_loop, daemon=True)
        self.monitor_thread.start()

    def stop_service(self):
        """Stops the background monitoring thread and disables active proxy."""
        if not self.is_running:
            return

        self.is_running = False
        self.stop_event.set()
        proxy_manager.disable_proxy()

        self.btn_toggle_service.configure(
            text="▶ START PROXY SERVICE",
            fg_color=("#10B981", "#059669"),
            hover_color=("#059669", "#047857")
        )
        self.service_badge.configure(
            text="● SERVICE STOPPED",
            fg_color=("#E5E7EB", "#374151"),
            text_color=("#6B7280", "#9CA3AF")
        )
        self.dash_match_status.configure(
            text="Idle (Service Stopped)",
            text_color="gray70"
        )
        self._refresh_live_status()
        self.log_message("⏹ ProProxy service STOPPED.")

    def _monitor_worker_loop(self):
        """
        Background thread loop that polls network adapters every 5 seconds.
        Detects active Ethernet and Wi-Fi networks and triggers proxy switch logic safely.
        """
        try:
            if not self.stop_event.is_set():
                self.after(0, self._check_and_switch_proxy)
        except Exception:
            return

        check_interval = self.config_mgr.get("check_interval", 5)

        while not self.stop_event.is_set():
            if self.stop_event.wait(timeout=check_interval):
                break

            if self.is_running and not self.stop_event.is_set():
                try:
                    self.after(0, self._check_and_switch_proxy)
                except Exception:
                    break

    def _check_and_switch_proxy(self):
        """
        Core network detection and state-tracking switching logic.
        1. Checks if connected via Ethernet -> Enables proxy.
        2. Checks if connected via monitored Wi-Fi -> Enables proxy.
        3. Otherwise (unmonitored Wi-Fi or disconnected) -> Disables proxy.
        Handles state correctly to prevent repeated redundant registry writes every 5s.
        """
        if self.is_quitting or not self.is_running:
            return

        net_info = network_monitor.get_active_network_info()
        is_eth = net_info.get("is_ethernet", False)
        net_type = net_info.get("type", "disconnected")
        current_ssid = net_info.get("ssid")

        proxy_ip = self.config_mgr.get("proxy_ip", "172.31.100.27")
        proxy_port = self.config_mgr.get("proxy_port", "3128")
        target_server = f"{proxy_ip}:{proxy_port}"
        auto_ethernet = self.config_mgr.get("enable_ethernet_proxy", True)

        proxy_state = proxy_manager.get_current_proxy_status()
        is_currently_enabled = proxy_state.get("enabled", False)
        current_server = proxy_state.get("server", "")

        # -------------------------------------------------------------
        # CASE 1: Ethernet Adapter Detected
        # -------------------------------------------------------------
        if is_eth and auto_ethernet:
            decision_key = f"eth:{target_server}"
            self.dash_match_status.configure(
                text="Matched 'Ethernet' -> ON",
                text_color=("#10B981", "#34D399")
            )
            self.dash_match_detail.configure(
                text=f"Ethernet (LAN) -> {target_server}"
            )

            if not is_currently_enabled or current_server != target_server:
                success, msg = proxy_manager.enable_proxy(proxy_ip, proxy_port)
                if success:
                    self.log_message(f"🔌 Connected to Ethernet (LAN) -> Enabled System Proxy ({target_server})")
                else:
                    self.log_message(f"❌ Failed to enable proxy: {msg}", "error")

            self.last_decision_state = decision_key

        # -------------------------------------------------------------
        # CASE 2: Wi-Fi Connection
        # -------------------------------------------------------------
        elif net_type == "wifi" and current_ssid:
            monitored_networks = self.config_mgr.get_networks()
            is_match = wifi_manager.is_wifi_matching(monitored_networks, current_ssid)

            if is_match:
                decision_key = f"wifi_match:{current_ssid}:{target_server}"
                self.dash_match_status.configure(
                    text=f"Matched '{current_ssid}' -> ON",
                    text_color=("#10B981", "#34D399")
                )
                self.dash_match_detail.configure(
                    text=f"Wi-Fi ({current_ssid}) -> {target_server}"
                )

                if not is_currently_enabled or current_server != target_server:
                    success, msg = proxy_manager.enable_proxy(proxy_ip, proxy_port)
                    if success:
                        self.log_message(f"📶 Connected to '{current_ssid}' (Match) -> Enabled System Proxy ({target_server})")
                    else:
                        self.log_message(f"❌ Failed to enable proxy: {msg}", "error")

                self.last_decision_state = decision_key
            else:
                decision_key = f"wifi_unmatch:{current_ssid}"
                self.dash_match_status.configure(
                    text=f"Unmatched '{current_ssid}' -> OFF",
                    text_color=("#F59E0B", "#FBBF24")
                )
                self.dash_match_detail.configure(
                    text="Not in monitored list -> Proxy disabled"
                )

                if is_currently_enabled:
                    success, msg = proxy_manager.disable_proxy()
                    if success:
                        self.log_message(f"🔴 Connected to '{current_ssid}' (Unmatched) -> Disabled System Proxy")

                self.last_decision_state = decision_key

        # -------------------------------------------------------------
        # CASE 3: Disconnected or Other Interface
        # -------------------------------------------------------------
        else:
            decision_key = "disconnected"
            self.dash_match_status.configure(
                text="Disconnected -> OFF",
                text_color="gray60"
            )
            self.dash_match_detail.configure(
                text="No active internet adapter"
            )

            if is_currently_enabled:
                success, msg = proxy_manager.disable_proxy()
                if success:
                    self.log_message("🔴 Disconnected from network -> Disabled System Proxy")

            self.last_decision_state = decision_key

        self._refresh_live_status()

        # Periodic background telemetry heartbeat sync
        now = time.time()
        if now - self.last_cloud_sync > self.cloud_sync_interval:
            self.last_cloud_sync = now
            current_tag = "Ethernet" if is_eth else current_ssid
            active_flag = proxy_manager.get_current_proxy_status().get("enabled", False)
            threading.Thread(
                target=lambda: cloud_manager.send_heartbeat(current_tag, active_flag, APP_VERSION),
                daemon=True
            ).start()

    # =========================================================================
    # LOGGING & UTILS
    # =========================================================================

    def log_message(self, message: str, level: str = "info"):
        """Appends a timestamped log entry to the activity log."""
        try:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            log_entry = f"[{timestamp}] {message}\n"

            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", log_entry)
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        except Exception:
            pass

    def clear_log(self):
        """Clears all entries from the activity log."""
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.delete("1.0", "end")
            self.log_textbox.configure(state="disabled")
        except Exception:
            pass


def main():
    """Application entry point with error safeguarding."""
    try:
        app = ProProxyApp()
        app.mainloop()
    except Exception as error:
        import traceback
        err_msg = traceback.format_exc()
        try:
            with open("proproxy_error.log", "w", encoding="utf-8") as f:
                f.write(err_msg)
        except Exception:
            pass
        try:
            tkmb.showerror("ProProxy Startup Notice", f"An unexpected error occurred:\n\n{error}\n\nCheck proproxy_error.log for details.")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
