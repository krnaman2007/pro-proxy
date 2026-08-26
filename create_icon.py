"""
Generates high-resolution application icon (.ico and .png) and system tray status icons.
Uses Pillow (PIL) to render crisp vector-like graphics.
"""

import os
from PIL import Image, ImageDraw, ImageFont


def create_app_icon(base_dir: str):
    """Generates icon.ico and icon.png for ProProxy."""
    size = (256, 256)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Outer rounded shield / badge background
    # Gradient-like rich indigo / dark slate circle
    margin = 12
    draw.rounded_rectangle(
        [margin, margin, 256 - margin, 256 - margin],
        radius=48,
        fill="#1E293B",  # Deep slate blue
        outline="#38BDF8",  # Sky blue accent outline
        width=6
    )

    # 2. Glowing inner network rings
    center_x, center_y = 128, 128
    
    # Outer antenna / Wi-Fi wave arc
    draw.arc([48, 48, 208, 208], start=210, end=330, fill="#0EA5E9", width=8)
    draw.arc([72, 72, 184, 184], start=210, end=330, fill="#38BDF8", width=8)
    draw.arc([96, 96, 160, 160], start=210, end=330, fill="#7DD3FC", width=8)

    # Center lightning / proxy bolt
    lightning_points = [
        (136, 100),
        (112, 138),
        (128, 138),
        (120, 180),
        (152, 130),
        (134, 130),
        (144, 100)
    ]
    draw.polygon(lightning_points, fill="#10B981", outline="#34D399")

    # Bottom dot / connection node
    draw.ellipse([120, 196, 136, 212], fill="#38BDF8")

    # Save PNG
    png_path = os.path.join(base_dir, "icon.png")
    img.save(png_path, format="PNG")

    # Save Multi-size ICO
    ico_path = os.path.join(base_dir, "icon.ico")
    icon_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(ico_path, format="ICO", sizes=icon_sizes)

    print(f"Generated {ico_path} and {png_path}")
    return ico_path, png_path


def create_tray_status_icons(base_dir: str):
    """Generates tray_on.png (green indicator) and tray_off.png (red indicator)."""
    for status, color, outline in [("on", "#10B981", "#059669"), ("off", "#EF4444", "#DC2626")]:
        size = (64, 64)
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Background circular badge
        draw.rounded_rectangle([4, 4, 60, 60], radius=16, fill="#0F172A", outline=color, width=3)

        # Network symbol
        draw.arc([16, 14, 48, 46], start=215, end=325, fill=color, width=3)
        draw.arc([22, 20, 42, 40], start=215, end=325, fill=color, width=3)

        # Status indicator dot
        draw.ellipse([28, 38, 36, 46], fill=color, outline=outline, width=1)

        filename = f"tray_{status}.png"
        img.save(os.path.join(base_dir, filename), format="PNG")

    print(f"Generated tray status icons in {base_dir}")


if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    create_app_icon(current_dir)
    create_tray_status_icons(current_dir)
