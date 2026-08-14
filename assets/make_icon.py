"""Generate the application icon asset (assets/app.ico)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(exist_ok=True)

SIZE = 256


def make_icon(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    r = int(size * 0.42)

    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(30, 30, 60))

    mic_w = int(size * 0.28)
    mic_h = int(size * 0.42)
    x0, y0 = cx - mic_w // 2, cy - mic_h // 2 + int(size * 0.03)
    d.rounded_rectangle((x0, y0, x0 + mic_w, y0 + mic_h), radius=mic_w // 2,
                        fill=(240, 90, 90))

    stand_w = int(size * 0.08)
    stand_h = int(size * 0.24)
    sx0, sy0 = cx - stand_w // 2, y0 + mic_h - int(size * 0.02)
    d.rounded_rectangle((sx0, sy0, sx0 + stand_w, sy0 + stand_h), radius=stand_w // 2,
                        fill=(200, 200, 220))

    arc_r = int(size * 0.16)
    d.arc((cx - arc_r, sy0 + stand_h - int(size * 0.02),
           cx + arc_r, sy0 + stand_h + arc_r * 2 - int(size * 0.02)),
          start=10, end=170, fill=(200, 200, 220), width=int(size * 0.05))
    return img


def main() -> None:
    icon = make_icon()
    icon.save(OUT / "app.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    icon.save(OUT / "app.png")
    print(f"Wrote {OUT / 'app.ico'} and {OUT / 'app.png'}")


if __name__ == "__main__":
    main()