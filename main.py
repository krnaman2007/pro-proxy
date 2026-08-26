"""
ProProxy - Main Application
A modern Windows 10 & 11 desktop application to automatically enable/disable
the Windows system proxy based on the currently connected Wi-Fi network SSID.

Features:
- System Tray integration with minimize-to-tray & dynamic status icons.
- Windows Startup support (Run on login via HKCU Run registry).
- Cloud Telemetry (Supabase Heartbeat) & Real-time Remote Control (Kill Switch & Announcements).
- Auto-Updater with GitHub Releases integration.
- Live Wi-Fi interface detection (netsh wlan show interfaces).
- Real-time proxy registry management (HKCU Internet Settings & WinINet flush).
- Non-blocking background monitoring daemon.
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
import proxy_manager
import updater
import wifi_manager

APP_VERSION = "1.0.0"

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

        # Cloud sync state
        self.last_cloud_sync = 0.0
        self.cloud_sync_interval = 600.0  # Sync with Supabase every 10 minutes

        # System Tray State
        self.tray_icon: Optional[pystray.Icon] = None
        self.tray_thread: Optional[threading.Thread] = None
        self._load_icons()

        # Network item UI tracking (ssid -> (checkbox_var, widget_frame))
        self.network_widgets: Dict[str, tuple[ctk.BooleanVar, ctk.CTkFrame]] = {}

        # --- Window Setup ---
        self.title(f"ProProxy v{APP_VERSION} - Auto Proxy Switcher")
        self.geometry("980x780")
        self.minsize(900, 700)

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
        wifi_info = wifi_manager.get_current_wifi()
        proxy_info = proxy_manager.get_current_proxy_status()
        cloud_manager.send_heartbeat(
            current_wifi=wifi_info.get("ssid"),
            proxy_status=proxy_info.get("enabled", False),
            app_version=APP_VERSION
        )

        # 3. Check for Updates via GitHub Releases
        update_info = updater.check_for_updates(
            current_version=APP_VERSION,
            github_repo=cloud_manager.GITHUB_REPO
        )
        if update_info.get("update_available"):
            self.after(0, lambda: self._prompt_update(update_info))

    def _trigger_kill_switch(self, message: str):
        """Displays kill switch notice and terminates the application."""
        self.deiconify()
        self.lift()
        tkmb.showerror(
            "ProProxy - Service Notice",
            f"⚠️ Application Disabled\n\n{message}\n\nProProxy will now close."
        )
        self.quit_application()

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

        # Run tray loop in dedicated background daemon thread
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def _get_tray_proxy_status_text(self) -> str:
        """Returns dynamic menu text for proxy status."""
        proxy_info = proxy_manager.get_current_proxy_status()
        if proxy_info.get("enabled"):
            server = proxy_info.get("server") or "Active"
            return f"Proxy: ON ({server})"
        return "Proxy: OFF (Disabled)"

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
            status_text = "Proxy: ON" if proxy_enabled else "Proxy: OFF"
            service_text = "Running" if self.is_running else "Stopped"
            self.tray_icon.title = f"ProProxy [{status_text} | {service_text}]"
        except Exception:
            pass

    def _tray_show_window(self, icon=None, item=None):
        """Restores and focuses the main window from the system tray."""
        self.after(0, self._restore_window)

    def _restore_window(self):
        """Brings the Tkinter window to front."""
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _tray_toggle_service(self, icon=None, item=None):
        """Toggles monitoring service from the tray menu."""
        self.after(0, self.toggle_service)

    def _tray_exit_app(self, icon=None, item=None):
        """Exits the application completely from tray menu."""
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

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        if self.monitor_thread and self.monitor_thread.is_alive():
            try:
                self.monitor_thread.join(timeout=0.5)
            except Exception:
                pass

        self.destroy()

    # =========================================================================
    # UI CONSTRUCTION
    # =========================================================================

    def _build_announcement_banner(self):
        """Builds top remote announcement banner (hidden by default)."""
        self.announcement_frame = ctk.CTkFrame(
            self,
            fg_color=("#FEF3C7", "#78350F"),
            corner_radius=8,
            height=32
        )
        self.announcement_label = ctk.CTkLabel(
            self.announcement_frame,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("#92400E", "#FDE68A")
        )
        self.announcement_label.pack(side="left", padx=16, pady=4)

    def _show_announcement(self, message: str):
        """Displays announcement banner at the top of the window."""
        self.announcement_label.configure(text=f"📢 Notice: {message}")
        self.announcement_frame.grid(row=0, column=0, padx=20, pady=(10, 0), sticky="ew")

    def _build_header(self):
        """Builds top header with title, subtitle, status pills, and theme menu."""
        header_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header_frame.grid(row=1, column=0, padx=20, pady=(12, 5), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)

        # Title & Subtitle container
        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")

        title_label = ctk.CTkLabel(
            title_box,
            text="⚡ ProProxy",
            font=ctk.CTkFont(size=22, weight="bold")
        )
        title_label.pack(anchor="w")

        subtitle_label = ctk.CTkLabel(
            title_box,
            text=f"v{APP_VERSION} • Automatic proxy switching based on connected Wi-Fi network",
            font=ctk.CTkFont(size=12),
            text_color="gray70"
        )
        subtitle_label.pack(anchor="w")

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

        # Card 1: Current Wi-Fi Status
        wifi_card = ctk.CTkFrame(dash_frame, fg_color=("gray90", "gray17"), corner_radius=10)
        wifi_card.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")

        ctk.CTkLabel(
            wifi_card,
            text="📶 CURRENT WI-FI",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="gray60"
        ).pack(anchor="w", padx=12, pady=(10, 2))

        self.dash_wifi_ssid = ctk.CTkLabel(
            wifi_card,
            text="Detecting...",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=("#2563EB", "#60A5FA")
        )
        self.dash_wifi_ssid.pack(anchor="w", padx=12, pady=(0, 2))

        self.dash_wifi_detail = ctk.CTkLabel(
            wifi_card,
            text="Checking interface...",
            font=ctk.CTkFont(size=11),
            text_color="gray60"
        )
        self.dash_wifi_detail.pack(anchor="w", padx=12, pady=(0, 10))

        # Card 2: System Proxy Status with Prominent Indicator
        proxy_card = ctk.CTkFrame(dash_frame, fg_color=("gray90", "gray17"), corner_radius=10)
        proxy_card.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")

        ctk.CTkLabel(
            proxy_card,
            text="🌐 SYSTEM PROXY STATUS",
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
            text="⚙️ SWITCH RULE DECISION",
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
        """Builds two-column area: Proxy settings on left, Wi-Fi list on right."""
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.grid(row=3, column=0, padx=20, pady=5, sticky="nsew")
        content_frame.grid_columnconfigure(0, weight=4)  # Left column
        content_frame.grid_columnconfigure(1, weight=5)  # Right column
        content_frame.grid_rowconfigure(0, weight=1)

        # ---------------------------------------------------------------------
        # LEFT PANEL: Proxy Settings & System Startup Controls
        # ---------------------------------------------------------------------
        left_panel = ctk.CTkFrame(content_frame, corner_radius=12)
        left_panel.grid(row=0, column=0, padx=(0, 10), pady=0, sticky="nsew")
        left_panel.grid_columnconfigure(0, weight=1)

        # Panel Title
        ctk.CTkLabel(
            left_panel,
            text="🔧 Proxy Configuration",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=16, pady=(14, 10))

        # Proxy IP Input
        ctk.CTkLabel(
            left_panel,
            text="Proxy IP / Hostname:",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=16, pady=(2, 2))

        self.entry_ip = ctk.CTkEntry(
            left_panel,
            placeholder_text="e.g. 172.31.100.110 or proxy.naman.com",
            height=34
        )
        self.entry_ip.pack(fill="x", padx=16, pady=(0, 8))
        self.entry_ip.bind("<KeyRelease>", lambda e: self._on_settings_modified())

        # Proxy Port Input
        ctk.CTkLabel(
            left_panel,
            text="Proxy Port:",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=16, pady=(2, 2))

        self.entry_port = ctk.CTkEntry(
            left_panel,
            placeholder_text="e.g. 3128",
            height=34
        )
        self.entry_port.pack(fill="x", padx=16, pady=(0, 10))
        self.entry_port.bind("<KeyRelease>", lambda e: self._on_settings_modified())

        # Checkboxes: Auto-start service & Start with Windows
        options_box = ctk.CTkFrame(left_panel, fg_color="transparent")
        options_box.pack(fill="x", padx=16, pady=(0, 8))

        self.chk_autostart = ctk.CTkCheckBox(
            options_box,
            text="Auto-start monitoring on launch",
            font=ctk.CTkFont(size=12),
            command=self._on_settings_modified
        )
        self.chk_autostart.pack(anchor="w", pady=(2, 4))

        self.chk_windows_startup = ctk.CTkCheckBox(
            options_box,
            text="Start with Windows (Launch on boot)",
            font=ctk.CTkFont(size=12),
            command=self._on_windows_startup_toggled
        )
        self.chk_windows_startup.pack(anchor="w", pady=(2, 4))

        self.chk_minimize_tray = ctk.CTkCheckBox(
            options_box,
            text="Minimize to System Tray on close",
            font=ctk.CTkFont(size=12),
            command=self._on_settings_modified
        )
        self.chk_minimize_tray.pack(anchor="w", pady=(2, 4))

        # Save & Refresh Button Row
        save_box = ctk.CTkFrame(left_panel, fg_color="transparent")
        save_box.pack(fill="x", padx=16, pady=(4, 10))
        save_box.grid_columnconfigure((0, 1), weight=1)

        self.btn_save = ctk.CTkButton(
            save_box,
            text="💾 Save Settings",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#3B82F6", "#2563EB"),
            hover_color=("#2563EB", "#1D4ED8"),
            height=32,
            command=self.save_settings
        )
        self.btn_save.grid(row=0, column=0, padx=(0, 5), sticky="ew")

        self.btn_refresh = ctk.CTkButton(
            save_box,
            text="🔄 Refresh Status",
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=32,
            command=self._refresh_live_status
        )
        self.btn_refresh.grid(row=0, column=1, padx=(5, 0), sticky="ew")

        # Separator Line
        separator = ctk.CTkFrame(left_panel, height=2, fg_color=("gray80", "gray25"))
        separator.pack(fill="x", padx=16, pady=6)

        # Service Control Section
        ctk.CTkLabel(
            left_panel,
            text="🚀 Service Controller",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(anchor="w", padx=16, pady=(4, 6))

        self.btn_toggle_service = ctk.CTkButton(
            left_panel,
            text="▶ START PROXY SERVICE",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("#10B981", "#059669"),
            hover_color=("#059669", "#047857"),
            height=42,
            corner_radius=10,
            command=self.toggle_service
        )
        self.btn_toggle_service.pack(fill="x", padx=16, pady=(0, 8))

        # Manual Testing Controls
        manual_box = ctk.CTkFrame(left_panel, fg_color="transparent")
        manual_box.pack(fill="x", padx=16, pady=(0, 8))
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
            left_panel,
            text="🔍 Check for Updates",
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            hover_color=("gray85", "gray25"),
            text_color=("gray40", "gray60"),
            height=24,
            command=self.check_updates_manual
        )
        self.btn_check_update.pack(fill="x", padx=16, pady=(0, 10))

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
            text="📡 Monitored Wi-Fi Networks",
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
            placeholder_text="Enter Wi-Fi SSID (e.g. MNNIT or Naman_5g)",
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

        # Network Management Action Row
        action_box = ctk.CTkFrame(right_panel, fg_color="transparent")
        action_box.grid(row=4, column=0, padx=16, pady=(0, 12), sticky="ew")
        action_box.grid_columnconfigure((0, 1, 2), weight=1)

        self.btn_add_current = ctk.CTkButton(
            action_box,
            text="➕ Add Current Wi-Fi",
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=28,
            command=self.add_current_wifi_to_list
        )
        self.btn_add_current.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_remove_selected = ctk.CTkButton(
            action_box,
            text="🗑️ Remove Selected",
            font=ctk.CTkFont(size=11),
            fg_color=("#EF4444", "#DC2626"),
            hover_color=("#DC2626", "#B91C1C"),
            height=28,
            command=self.remove_selected_networks
        )
        self.btn_remove_selected.grid(row=0, column=1, padx=4, sticky="ew")

        self.btn_clear_all = ctk.CTkButton(
            action_box,
            text="Clear All",
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            height=28,
            command=self.clear_all_networks
        )
        self.btn_clear_all.grid(row=0, column=2, padx=(4, 0), sticky="ew")

    def _build_log_area(self):
        """Builds bottom activity log console."""
        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.grid(row=4, column=0, padx=20, pady=(5, 15), sticky="ew")
        log_frame.grid_columnconfigure(0, weight=1)

        # Log Header
        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=16, pady=(10, 4))
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header,
            text="📋 Activity Log",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            log_header,
            text="Clear Log",
            font=ctk.CTkFont(size=10),
            width=70,
            height=22,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self.clear_log
        ).grid(row=0, column=1, sticky="e")

        # Log Text Box
        self.log_textbox = ctk.CTkTextbox(
            log_frame,
            height=95,
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=8,
            activate_scrollbars=True
        )
        self.log_textbox.pack(fill="x", padx=16, pady=(0, 10))
        self.log_textbox.configure(state="disabled")

    # =========================================================================
    # SETTINGS & DATA BINDING
    # =========================================================================

    def _load_settings_into_ui(self):
        """Loads settings from ConfigManager and Registry into the UI inputs."""
        ip_val = self.config_mgr.get("proxy_ip", "172.31.100.110")
        port_val = str(self.config_mgr.get("proxy_port", "3128"))

        self.entry_ip.delete(0, "end")
        self.entry_ip.insert(0, ip_val)

        self.entry_port.delete(0, "end")
        self.entry_port.insert(0, port_val)

        if self.config_mgr.get("auto_start", True):
            self.chk_autostart.select()
        else:
            self.chk_autostart.deselect()

        registry_startup = is_autostart_enabled()
        if registry_startup or self.config_mgr.get("start_with_windows", False):
            self.chk_windows_startup.select()
        else:
            self.chk_windows_startup.deselect()

        if self.config_mgr.get("minimize_to_tray", True):
            self.chk_minimize_tray.select()
        else:
            self.chk_minimize_tray.deselect()

        self._refresh_network_list_ui()

    def _refresh_network_list_ui(self):
        """Re-renders the scrollable list of monitored Wi-Fi SSIDs."""
        for widget in self.networks_scroll_frame.winfo_children():
            widget.destroy()
        self.network_widgets.clear()

        networks = self.config_mgr.get_networks()

        if not networks:
            empty_label = ctk.CTkLabel(
                self.networks_scroll_frame,
                text="No Wi-Fi networks saved yet.\nEnter an SSID above or click 'Add Current Wi-Fi'.",
                font=ctk.CTkFont(size=12, slant="italic"),
                text_color="gray60",
                pady=25
            )
            empty_label.pack(fill="x")
            return

        for index, ssid in enumerate(networks):
            item_frame = ctk.CTkFrame(
                self.networks_scroll_frame,
                fg_color=("gray95", "gray22"),
                corner_radius=6,
                height=36
            )
            item_frame.pack(fill="x", padx=4, pady=3)
            item_frame.grid_columnconfigure(1, weight=1)

            check_var = ctk.BooleanVar(value=False)
            checkbox = ctk.CTkCheckBox(
                item_frame,
                text="",
                variable=check_var,
                width=24,
                checkbox_width=18,
                checkbox_height=18
            )
            checkbox.grid(row=0, column=0, padx=(10, 5), pady=6, sticky="w")

            ssid_label = ctk.CTkLabel(
                item_frame,
                text=f"📶  {ssid}",
                font=ctk.CTkFont(size=13, weight="normal"),
                anchor="w"
            )
            ssid_label.grid(row=0, column=1, padx=5, pady=6, sticky="w")

            del_btn = ctk.CTkButton(
                item_frame,
                text="✕",
                width=28,
                height=24,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="transparent",
                text_color=("#EF4444", "#F87171"),
                hover_color=("gray85", "gray30"),
                command=lambda s=ssid: self.remove_single_network(s)
            )
            del_btn.grid(row=0, column=2, padx=(5, 8), pady=6, sticky="e")

            self.network_widgets[ssid] = (check_var, item_frame)

    def _on_settings_modified(self):
        """Auto-saves changes to configuration when inputs change."""
        self._sync_ui_to_config(auto_save=True)

    def _on_windows_startup_toggled(self):
        """Handles user clicking the 'Start with Windows' checkbox."""
        enable_startup = bool(self.chk_windows_startup.get())
        success, msg = set_autostart(enable_startup)
        if success:
            self.config_mgr.set("start_with_windows", enable_startup, auto_save=True)
            self.log_message(f"⚙️ Windows Startup: {msg}")
        else:
            self.log_message(f"❌ Failed to configure Windows startup: {msg}", "error")
            if is_autostart_enabled():
                self.chk_windows_startup.select()
            else:
                self.chk_windows_startup.deselect()

    def _sync_ui_to_config(self, auto_save: bool = True) -> bool:
        """Syncs UI inputs into ConfigManager."""
        ip = self.entry_ip.get().strip()
        port = self.entry_port.get().strip()
        auto_start = bool(self.chk_autostart.get())
        minimize_tray = bool(self.chk_minimize_tray.get())

        self.config_mgr.set("proxy_ip", ip, auto_save=False)
        self.config_mgr.set("proxy_port", port, auto_save=False)
        self.config_mgr.set("auto_start", auto_start, auto_save=False)
        self.config_mgr.set("minimize_to_tray", minimize_tray, auto_save=False)

        if auto_save:
            return self.config_mgr.save_settings()
        return True

    def save_settings(self):
        """Validates and explicitly saves settings to settings.json."""
        ip = self.entry_ip.get().strip()
        port = self.entry_port.get().strip()

        if not ConfigManager.validate_ip(ip):
            self.log_message(f"⚠️ Warning: '{ip}' may not be a valid IP or hostname.", "warning")

        if not ConfigManager.validate_port(port):
            self.log_message(f"❌ Error: Port '{port}' is invalid (must be 1-65535).", "error")
            return

        success = self._sync_ui_to_config(auto_save=True)
        if success:
            self.log_message(f"💾 Settings saved to settings.json ({ip}:{port}).")
            self._flash_button(self.btn_save, "Saved! ✓")
        else:
            self.log_message("❌ Failed to save settings.json.", "error")

    def _flash_button(self, button: ctk.CTkButton, temporary_text: str):
        """Briefly changes button text for visual feedback."""
        original_text = button.cget("text")
        button.configure(text=temporary_text)
        self.after(1500, lambda: button.configure(text=original_text))

    def _on_theme_change(self, new_theme: str):
        """Handles theme mode change."""
        ctk.set_appearance_mode(new_theme)
        self.config_mgr.set("theme", new_theme, auto_save=True)
        self.log_message(f"Appearance theme set to '{new_theme}'.")

    # =========================================================================
    # NETWORK LIST ACTIONS
    # =========================================================================

    def add_network_from_entry(self):
        """Adds the network SSID typed in the entry box."""
        ssid = self.entry_new_ssid.get().strip()
        if not ssid:
            return

        if self.config_mgr.add_network(ssid):
            self.log_message(f"➕ Added Wi-Fi network to monitor list: '{ssid}'")
            self.entry_new_ssid.delete(0, "end")
            self._refresh_network_list_ui()
            if self.is_running:
                self._check_and_switch_proxy()
        else:
            self.log_message(f"⚠️ Network '{ssid}' is already in the list.")

    def add_current_wifi_to_list(self):
        """Shortcut to detect and add currently connected Wi-Fi to list."""
        wifi_info = wifi_manager.get_current_wifi()
        if wifi_info.get("connected") and wifi_info.get("ssid"):
            ssid = wifi_info["ssid"]
            if self.config_mgr.add_network(ssid):
                self.log_message(f"➕ Added current Wi-Fi '{ssid}' to monitor list.")
                self._refresh_network_list_ui()
                if self.is_running:
                    self._check_and_switch_proxy()
            else:
                self.log_message(f"ℹ️ Current Wi-Fi '{ssid}' is already in the list.")
        else:
            self.log_message("⚠️ No connected Wi-Fi detected to add.")

    def remove_single_network(self, ssid: str):
        """Removes a single SSID from the monitored list."""
        if self.config_mgr.remove_network(ssid):
            self.log_message(f"🗑️ Removed Wi-Fi network: '{ssid}'")
            self._refresh_network_list_ui()
            if self.is_running:
                self._check_and_switch_proxy()

    def remove_selected_networks(self):
        """Removes all checked networks in the list."""
        selected = [
            ssid for ssid, (var, _) in self.network_widgets.items()
            if var.get()
        ]
        if not selected:
            self.log_message("ℹ️ No networks selected for removal.")
            return

        current_networks = self.config_mgr.get_networks()
        updated = [net for net in current_networks if net not in selected]
        self.config_mgr.set_networks(updated)
        self.log_message(f"🗑️ Removed {len(selected)} selected network(s).")
        self._refresh_network_list_ui()
        if self.is_running:
            self._check_and_switch_proxy()

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
        # 1. Query Wi-Fi
        wifi_info = wifi_manager.get_current_wifi()
        if wifi_info.get("connected") and wifi_info.get("ssid"):
            ssid = wifi_info["ssid"]
            signal = wifi_info.get("signal", "")
            signal_str = f" ({signal})" if signal else ""
            self.dash_wifi_ssid.configure(
                text=f"{ssid}{signal_str}",
                text_color=("#2563EB", "#60A5FA")
            )
            self.dash_wifi_detail.configure(text=f"Interface: {wifi_info.get('interface_name', 'Wi-Fi')} | Connected")
        elif wifi_info.get("state") == "no_interface":
            self.dash_wifi_ssid.configure(text="No Wi-Fi Adapter", text_color="gray60")
            self.dash_wifi_detail.configure(text="No wireless card detected")
        else:
            self.dash_wifi_ssid.configure(text="Disconnected", text_color="gray60")
            self.dash_wifi_detail.configure(text="Wi-Fi not connected")

        # 2. Query System Proxy
        proxy_info = proxy_manager.get_current_proxy_status()
        is_proxy_enabled = proxy_info.get("enabled", False)

        if is_proxy_enabled:
            server = proxy_info.get("server") or "Active"
            self.dash_proxy_status.configure(
                text="🟢 PROXY ENABLED",
                text_color=("#10B981", "#34D399")
            )
            self.dash_proxy_server.configure(text=f"Active Server: {server}")
        else:
            self.dash_proxy_status.configure(
                text="🔴 PROXY DISABLED",
                text_color=("#EF4444", "#F87171")
            )
            server = proxy_info.get("server")
            self.dash_proxy_server.configure(
                text=f"Last Server: {server}" if server else "Server: None"
            )

        # Update system tray icon
        self._update_tray_icon(is_proxy_enabled)

    def _manual_enable_proxy(self):
        """Manually enables system proxy with current UI settings."""
        ip = self.entry_ip.get().strip()
        port = self.entry_port.get().strip()
        if not ConfigManager.validate_port(port):
            self.log_message(f"❌ Invalid port: {port}", "error")
            return

        success, msg = proxy_manager.enable_proxy(ip, port)
        if success:
            self.log_message(f"🌐 [Manual] {msg}")
        else:
            self.log_message(f"❌ [Manual] {msg}", "error")
        self._refresh_live_status()

    def _manual_disable_proxy(self):
        """Manually disables system proxy."""
        success, msg = proxy_manager.disable_proxy()
        if success:
            self.log_message(f"🌐 [Manual] {msg}")
        else:
            self.log_message(f"❌ [Manual] {msg}", "error")
        self._refresh_live_status()

    # =========================================================================
    # BACKGROUND SERVICE & MONITORING LOOP
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

        self.log_message("▶ ProProxy service STARTED. Monitoring network every 5s...")

        self.monitor_thread = threading.Thread(target=self._monitor_worker_loop, daemon=True)
        self.monitor_thread.start()

    def stop_service(self):
        """Stops the background monitoring thread."""
        if not self.is_running:
            return

        self.is_running = False
        self.stop_event.set()

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

        self.log_message("⏹ ProProxy service STOPPED.")

    def _monitor_worker_loop(self):
        """
        Background thread loop that runs every 5 seconds.
        Detects Wi-Fi network and triggers proxy switch logic safely.
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
        Core detection and switching logic executed on UI thread.
        Detects current Wi-Fi and enables/disables proxy accordingly.
        """
        if self.is_quitting:
            return

        wifi_info = wifi_manager.get_current_wifi()
        is_connected = wifi_info.get("connected", False)
        current_ssid = wifi_info.get("ssid")

        monitored_networks = self.config_mgr.get_networks()
        is_match = wifi_manager.is_wifi_matching(monitored_networks, current_ssid)

        proxy_ip = self.config_mgr.get("proxy_ip", "172.31.100.110")
        proxy_port = self.config_mgr.get("proxy_port", "3128")

        # Update Dashboard Status
        self._refresh_live_status()

        if is_connected and current_ssid:
            if is_match:
                self.dash_match_status.configure(
                    text=f"Matched '{current_ssid}' -> ON",
                    text_color=("#10B981", "#34D399")
                )
                self.dash_match_detail.configure(
                    text=f"Target: {proxy_ip}:{proxy_port}"
                )

                proxy_state = proxy_manager.get_current_proxy_status()
                target_server = f"{proxy_ip}:{proxy_port}"

                if not proxy_state.get("enabled") or proxy_state.get("server") != target_server:
                    success, msg = proxy_manager.enable_proxy(proxy_ip, proxy_port)
                    if success:
                        self.log_message(f"🟢 Connected to '{current_ssid}' (Match) -> Enabled Proxy ({target_server})")
                    else:
                        self.log_message(f"❌ Failed to enable proxy: {msg}", "error")
                    self._refresh_live_status()
            else:
                self.dash_match_status.configure(
                    text=f"Unmatched '{current_ssid}' -> OFF",
                    text_color=("#F59E0B", "#FBBF24")
                )
                self.dash_match_detail.configure(
                    text="Not in monitored list -> Proxy disabled"
                )

                proxy_state = proxy_manager.get_current_proxy_status()
                if proxy_state.get("enabled"):
                    success, msg = proxy_manager.disable_proxy()
                    if success:
                        self.log_message(f"🔴 Connected to '{current_ssid}' (Unmatched) -> Disabled Proxy")
                    else:
                        self.log_message(f"❌ Failed to disable proxy: {msg}", "error")
                    self._refresh_live_status()
        else:
            self.dash_match_status.configure(
                text="Disconnected -> OFF",
                text_color="gray60"
            )
            self.dash_match_detail.configure(
                text="No active Wi-Fi connection"
            )

            proxy_state = proxy_manager.get_current_proxy_status()
            if proxy_state.get("enabled"):
                success, msg = proxy_manager.disable_proxy()
                if success:
                    self.log_message("🔴 Disconnected from Wi-Fi -> Disabled Proxy")
                else:
                    self.log_message(f"❌ Failed to disable proxy: {msg}", "error")
                self._refresh_live_status()

        # Periodic background telemetry heartbeat sync (every 10 minutes)
        now = time.time()
        if now - self.last_cloud_sync > self.cloud_sync_interval:
            self.last_cloud_sync = now
            current_proxy = proxy_manager.get_current_proxy_status().get("enabled", False)
            threading.Thread(
                target=lambda: cloud_manager.send_heartbeat(current_ssid, current_proxy, APP_VERSION),
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
    """Application entry point."""
    app = ProProxyApp()
    app.mainloop()


if __name__ == "__main__":
    main()

