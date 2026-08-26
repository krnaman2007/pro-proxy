# ⚡ ProProxy - Auto Proxy Switcher (Windows 10 & 11)

A modern, automated Windows desktop application built with **Python**, **CustomTkinter**, **pystray**, and **Supabase Cloud Telemetry** that automatically manages Windows proxy settings, reports live user analytics, and supports remote control.

---

## 🌟 Features

1. **Live User Telemetry & Active User Count (Supabase)**:
   - Registers each installation with an anonymous hardware device ID.
   - Pings Supabase automatically every 10 minutes.
   - View total users and active users in real-time on your Supabase dashboard!

2. **Remote App Control & Kill Switch**:
   - **Kill Switch**: If you set `app_enabled = false` in your Supabase `remote_config` table, the app displays a maintenance alert and deactivates itself immediately.
   - **Remote Announcements**: Broadcast live messages/banners to all running applications.
   - **Remote Default Proxy Change**: Change default proxy IPs remotely.

3. **Auto-Updater (GitHub Releases)**:
   - Detects new `.exe` versions released on GitHub and updates silently in the background.

4. **System Tray & Windows Startup**:
   - Runs in the Windows System Tray with dynamic 🟢 Green (ON) and 🔴 Red (OFF) indicators.
   - "Start with Windows" checkbox configures automatic startup on login.

5. **Standalone `.EXE` Distribution**:
   - Single-file binary: `dist/ProProxy.exe`.
   - Your Python code and dependencies are securely compiled inside.

---

## ☁️ Connecting Your Free Supabase Backend

Open [`cloud_manager.py`](file:///c:/Users/naman/OneDrive/Desktop/proxy%20app/cloud_manager.py) and paste your Supabase credentials:

```python
SUPABASE_URL = "https://your-project-id.supabase.co"
SUPABASE_KEY = "your-anon-public-key"
GITHUB_REPO = "your-username/pro-proxy"  # (Optional: for GitHub Releases auto-updater)
```

Then rebuild the executable by running:
```powershell
python build_exe.py
```
*(Or double-click `build_exe.bat`)*

---

## 🚀 How to Run & Distribute

- **To run**: Open `dist/ProProxy.exe`.
- **To distribute**: Send `dist/ProProxy.exe` to anyone. No Python installation required on their PC.
