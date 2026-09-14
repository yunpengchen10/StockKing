"""Render Stock King's original vector K monogram for web and Windows.
Geometry is shared by SVG and antialiased PNG/ICO outputs.
"""
from pathlib import Path
from PIL import Image, ImageDraw
ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = "#101827"
BLUE = "#5288ff"
WHITE = "#edf3ff"
SHAPES = [
    (BLUE, [(23, 24), (35, 24), (35, 72), (23, 72)]),
    (WHITE, [(42, 43), (61, 24), (77, 24), (51, 50), (42, 50)]),
    (BLUE, [(42, 51), (52, 51), (77, 72), (60, 72), (42, 57)]),
]
def svg_mark():
    paths = "".join(f'<polygon fill="{color}" points="' + " ".join(f"{x},{y}" for x, y in points) + '"/>' for color, points in SHAPES)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" role="img" aria-label="Stock King"><rect x="1" y="1" width="94" height="94" rx="23" fill="{BACKGROUND}" stroke="#2b3b57"/>{paths}</svg>\n'
def render(size):
    scale = 16
    image = Image.new("RGBA", (96 * scale, 96 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((scale, scale, 95 * scale, 95 * scale), radius=23 * scale, fill=BACKGROUND, outline="#2b3b57", width=scale)
    for color, points in SHAPES:
        draw.polygon([(x * scale, y * scale) for x, y in points], fill=color)
    return image.resize((size, size), Image.Resampling.LANCZOS)
def main():
    for relative in ("branding/stock-king-mark.svg", "desktop/frontend/src/assets/images/stock-king-mark.svg"):
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg_mark(), encoding="utf-8")
    for relative, size in {"branding/stock-king-logo.png": 1024, "desktop/build/appicon.png": 512, "desktop/frontend/src/assets/images/logo-universal.png": 512}.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        render(size).save(path, optimize=True)
    for relative in ("desktop/build/app.ico", "desktop/build/windows/icon.ico"):
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        render(256).save(path, format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print("Stock King SVG, PNG and multi-resolution ICO assets generated.")
if __name__ == "__main__":
    main()
