"""Build-time helper: draws the Debrief icon (icon.png for the window, debrief.ico for the shortcut).

    python3 -m debrief.desktop.make_icon      (needs Pillow; the shipped files are already built)
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def draw(size=1024) -> Image.Image:
    s = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=52 * s, fill=(15, 18, 20, 255))
    # a "D" drawn as a racing line: straight on the left, one long corner on the right
    w = 26 * s
    x0, y0, y1 = 72 * s, 60 * s, 196 * s
    d.line([(x0, y0), (x0, y1)], fill=(238, 240, 238, 255), width=int(w))
    d.arc([x0 - 58 * s, y0, x0 + 116 * s, y1], start=-90, end=90, fill=(57, 135, 229, 255), width=int(w))
    d.line([(x0 - w / 2, y0 + w / 2 - 1), (x0 + 30 * s, y0 + w / 2 - 1)], fill=(57, 135, 229, 255), width=int(w))
    d.line([(x0 - w / 2, y1 - w / 2 + 1), (x0 + 30 * s, y1 - w / 2 + 1)], fill=(57, 135, 229, 255), width=int(w))
    # apex marker
    ax, ay, r = x0 + 116 * s - w / 2, (y0 + y1) / 2, 17 * s
    d.ellipse([ax - r, ay - r, ax + r, ay + r], fill=(235, 104, 52, 255), outline=(15, 18, 20, 255), width=int(5 * s))
    return im.resize((256, 256), Image.LANCZOS)


def main():
    im = draw()
    im.save(HERE / "icon.png", optimize=True)
    # ICO with PNG-compressed frames; the 256 px frame is what the desktop shortcut uses
    im.save(HERE / "debrief.ico", format="ICO", sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("wrote", HERE / "icon.png", "and", HERE / "debrief.ico")


if __name__ == "__main__":
    main()
