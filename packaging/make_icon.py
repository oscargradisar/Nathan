"""Draw packaging/nathan.png (run once; the PNG is committed). PyInstaller turns it into
.ico on Windows and .icns on macOS."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(37, 99, 235))
try:
    font = ImageFont.truetype("arialbd.ttf", 640)
except OSError:
    font = ImageFont.load_default(size=640)
d.text((S / 2, S / 2 + 20), "N", font=font, fill="white", anchor="mm")
img.save(Path(__file__).with_name("nathan.png"))
img.save(Path(__file__).with_name("nathan.ico"), sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)])
