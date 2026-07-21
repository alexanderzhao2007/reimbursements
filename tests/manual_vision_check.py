"""Manual live check for services.vision.extract_receipt.

This is NOT a unit test — it makes ONE real claude-sonnet-5 API call (billed) to
confirm the Claude Vision integration works end to end (model id, auth, prompt,
structured-output parsing, Pydantic validation). The mocked unit tests planned
for this module belong in tests/test_vision.py.

Usage (from the repo root):
    python -m tests.manual_vision_check                     # synthetic receipt
    python -m tests.manual_vision_check path/to/receipt.jpg  # your own image
"""

import io
import sys
from pathlib import Path

# Allow both `python -m tests.manual_vision_check` and
# `python tests/manual_vision_check.py` by ensuring the repo root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.vision import extract_receipt
from utils.image import is_supported, media_type_for, to_png


def _synthetic_png() -> bytes:
    """A small, legible fake receipt rendered to PNG bytes in memory."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (380, 240), "white")
    draw = ImageDraw.Draw(img)
    lines = [
        "BLUE BOTTLE COFFEE",
        "123 Main St, Berkeley CA",
        "2026-07-19",
        "",
        "Latte           4.50",
        "Croissant       3.25",
        "",
        "Subtotal        7.75",
        "Tax             0.70",
        "TOTAL           8.45",
    ]
    for i, line in enumerate(lines):
        draw.text((15, 12 + i * 21), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _media_type_from_bytes(image_bytes: bytes) -> tuple[bytes, str]:
    """Detect the real image format from the bytes (NOT the file extension, which
    can lie — e.g. a .jpg that is actually WebP) and return
    (possibly-converted bytes, media_type). Unsupported formats are converted to
    PNG, exactly as the real upload pipeline will."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        fmt = (im.format or "").lower()  # 'jpeg', 'png', 'gif', 'webp', ...
    if is_supported(fmt):
        return image_bytes, media_type_for(fmt)
    print(f"'{fmt or 'unknown'}' is not a Vision-supported type; converting to PNG.")
    return to_png(image_bytes), media_type_for("png")


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        image_bytes = path.read_bytes()
        print(f"Parsing {path} ...")
    else:
        image_bytes = _synthetic_png()
        print("Parsing synthetic receipt (pass an image path to use your own) ...")

    image_bytes, media_type = _media_type_from_bytes(image_bytes)
    result = extract_receipt(image_bytes, media_type)

    print("\nextract_receipt OK - validated ReceiptExtraction:")
    print("  vendor  :", result.vendor)
    print("  date    :", result.date)
    print("  total   :", result.total)
    print("  currency:", result.currency)
    print("  category:", result.category)
    print("  tax     :", result.tax)
    print("  line_items:")
    for item in result.line_items or []:
        print(f"    - {item.description}: {item.amount} x{item.quantity}")


if __name__ == "__main__":
    main()
