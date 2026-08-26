"""
Build Script for ProProxy
Compiles the application into a single standalone Windows executable (ProProxy.exe)
using PyInstaller with icon, cloud, updater, sing-box, and tun2socks assets embedded.
Requests Administrator privileges directly via embedded UAC manifest (--uac-admin).
"""

import os
import subprocess
import sys
import tempfile
import time
import PyInstaller.__main__


def build_executable():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(base_dir, "main.py")
    icon_path = os.path.join(base_dir, "icon.ico")
    dist_dir = os.path.join(base_dir, "dist")
    work_dir = os.path.join(tempfile.gettempdir(), "proproxy_pyi_build")
    output_exe = os.path.join(dist_dir, "ProProxy.exe")

    # Force terminate non-elevated instances of ProProxy.exe before building
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ProProxy.exe"], capture_output=True)
            time.sleep(0.5)
        except Exception:
            pass

    # Ensure output_exe is freed prior to packaging (rotate if in use)
    if os.path.exists(output_exe):
        try:
            os.remove(output_exe)
        except Exception:
            try:
                old_backup = os.path.join(dist_dir, "ProProxy.old.exe")
                if os.path.exists(old_backup):
                    try:
                        os.remove(old_backup)
                    except Exception:
                        pass
                os.rename(output_exe, old_backup)
            except Exception as e:
                print(f"[Build] Note on rotating existing binary: {e}")

    # Ensure icon exists before building
    if not os.path.exists(icon_path):
        import create_icon
        create_icon.create_app_icon(base_dir)
        create_icon.create_tray_status_icons(base_dir)

    print("==================================================")
    print("Building ProProxy Standalone Executable...")
    print("==================================================")

    # PyInstaller arguments
    args = [
        main_script,
        "--name=ProProxy",
        "--onefile",             # Package everything into a single .exe
        "--noconsole",           # Windowed application (no black console window)
        "--uac-admin",           # Request Administrator privileges directly on app launch
        f"--icon={icon_path}",   # Embed application icon
        f"--distpath={dist_dir}",
        f"--workpath={work_dir}",
        "--collect-all=customtkinter",
        "--collect-all=pystray",
        "--collect-all=PIL",
        "--collect-all=requests",
        f"--add-data={os.path.join(base_dir, 'icon.ico')};.",
        f"--add-data={os.path.join(base_dir, 'icon.png')};.",
        f"--add-data={os.path.join(base_dir, 'tray_on.png')};.",
        f"--add-data={os.path.join(base_dir, 'tray_off.png')};.",
        f"--add-binary={os.path.join(base_dir, 'bin', 'sing-box.exe')};bin",
        f"--add-binary={os.path.join(base_dir, 'bin', 'tun2socks.exe')};bin",
        f"--add-binary={os.path.join(base_dir, 'bin', 'wintun.dll')};bin",
        f"--add-data={os.path.join(base_dir, 'bin', 'sing-box.exe')};bin",
        f"--add-data={os.path.join(base_dir, 'bin', 'tun2socks.exe')};bin",
        f"--add-data={os.path.join(base_dir, 'bin', 'wintun.dll')};bin",
        "--clean",
        "-y"
    ]

    PyInstaller.__main__.run(args)

    if os.path.exists(output_exe):
        size_mb = os.path.getsize(output_exe) / (1024 * 1024)
        print("==================================================")
        print(" Build Successful!")
        print(f" Executable: {output_exe} ({size_mb:.2f} MB)")
        print(" You can distribute this single ProProxy.exe file to anyone.")
        print(" Your source code is securely compiled inside the executable.")
        print("==================================================")
    else:
        print("Build finished, check dist/ directory.")


if __name__ == "__main__":
    build_executable()
