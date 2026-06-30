#!/usr/bin/env bash
# Generate Zentra Grafana branding assets from og-logo.png (transparent PNG).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
LOGO="${ROOT}/og-logo.png"
OUT="${ROOT}/branding/generated"

if [ ! -f "$LOGO" ]; then
  echo "Logo not found: $LOGO" >&2
  exit 1
fi

mkdir -p "$OUT"

docker run --rm -v "${ROOT}:/work" python:3.12-slim bash -c '
pip install -q pillow
python << "PY"
from PIL import Image
import base64, os

logo_path = "/work/og-logo.png"
out = "/work/branding/generated"
os.makedirs(out, exist_ok=True)


def load_trimmed(path):
    img = Image.open(path).convert("RGBA")
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def fit_rgba(img, size):
    w, h = size
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    iw, ih = img.size
    scale = min(w / iw, h / ih)
    nw = max(1, int(iw * scale))
    nh = max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2), resized)
    return canvas


def save_png(canvas, path):
    canvas.save(path, format="PNG")


def png_canvas_to_svg(canvas, out_path):
    import io
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    w, h = canvas.size
    svg = (
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" "
        f"xmlns:xlink=\"http://www.w3.org/1999/xlink\" "
        f"width=\"{w}\" height=\"{h}\" viewBox=\"0 0 {w} {h}\">"
        f"<image width=\"{w}\" height=\"{h}\" "
        f"xlink:href=\"data:image/png;base64,{b64}\"/></svg>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)


logo = load_trimmed(logo_path)

# Grafana 13 slots (contain-fit, transparent background)
slots = {
    "grafana_icon.svg": (100, 100),           # login + sidebar icon
    "grafana_typelogo.svg": (280, 120),       # login wordmark (if used)
    "grafana_text_logo_dark.svg": (160, 48),  # header (dark theme)
    "grafana_text_logo_light.svg": (160, 48), # header (light theme)
}

for name, size in slots.items():
    canvas = fit_rgba(logo, size)
    png_canvas_to_svg(canvas, os.path.join(out, name))

for size, name in ((32, "fav32.png"), (16, "fav16.png"), (180, "apple-touch-icon.png")):
    save_png(fit_rgba(logo, (size, size)), os.path.join(out, name))

print("Generated branding from og-logo.png")
for f in sorted(os.listdir(out)):
    print(" ", f)
PY
'

echo "Branding assets written to ${OUT}"
