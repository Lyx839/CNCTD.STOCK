"""Erzeugt assets/cnctd_stock.ico und assets/icon.png aus icon.jpg.

Das Quellbild wird leicht beschnitten (unsaubere Screenshot-Ränder) und erhält
eine eigene, transparente Abrundung der Ecken.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "icon.jpg"
ASSETS = ROOT / "assets"

CROP_BOX = (12, 14, 1154, 1156)   # 1142 x 1142
RADIUS_RATIO = 0.225
SUPERSAMPLE = 4


def rounded_mask(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1),
                                           radius=int(big * RADIUS_RATIO), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    img = Image.open(SRC).convert("RGB").crop(CROP_BOX)
    size = img.width
    rgba = img.convert("RGBA")
    rgba.putalpha(rounded_mask(size))

    master = rgba.resize((512, 512), Image.LANCZOS)
    master.save(ASSETS / "icon.png")
    master.save(ASSETS / "cnctd_stock.ico",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Icon erzeugt:", ASSETS / "cnctd_stock.ico")


if __name__ == "__main__":
    main()
