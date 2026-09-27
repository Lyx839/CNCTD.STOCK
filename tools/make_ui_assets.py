"""Erzeugt kleine UI-Grafiken (Häkchen, Pfeile) für das Stylesheet in assets/."""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
S = 8  # Supersampling


def _save(img: Image.Image, name: str, size: int) -> None:
    img.resize((size, size), Image.LANCZOS).save(ASSETS / name)


def polyline(points, color, width, size, name):
    img = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = [(x * S, y * S) for x, y in points]
    d.line(pts, fill=color, width=int(width * S), joint="curve")
    r = width * S / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)
    _save(img, name, size)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    white = (255, 255, 255, 255)
    grey = (91, 100, 117, 255)
    polyline([(3.5, 8.5), (6.5, 11.5), (12.5, 4.5)], white, 2.2, 16, "check.png")
    polyline([(4, 6), (8, 10), (12, 6)], grey, 1.8, 16, "chevron_down.png")
    polyline([(4, 10), (8, 6), (12, 10)], grey, 1.8, 16, "chevron_up.png")
    print("UI-Grafiken erzeugt in", ASSETS)


if __name__ == "__main__":
    main()
