"""Image utilities: normalization for OCR, SHA-256 hashing, base64.

Claude Vision accepts JPEG, PNG, GIF, and WebP directly, but a raw phone photo —
even in a supported format — reads poorly: it carries its rotation in an EXIF tag
(pixels stay sideways), is often far larger than the model's ~1568px working size,
and suffers glare/shadow a flatbed scan never has. So EVERY image is normalized
through Pillow before it reaches the model (upright → right-sized → contrast-
evened → PNG), not just the unsupported formats (HEIC, TIFF, BMP, …). Dedup
hashing is done by the caller on the *final* normalized bytes, matching the
pipeline order in ARCHITECTURE.md (convert → hash).
"""

import base64
import hashlib
import io

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

# Teach Pillow to decode HEIC/HEIF (the iPhone default). Without this, Image.open
# raises UnidentifiedImageError on a .heic upload.
register_heif_opener()

# Filetypes Claude Vision accepts directly (Slack `filetype` field values).
SUPPORTED_FILETYPES = {"jpg", "jpeg", "png", "gif", "webp"}

# Slack filetype -> Anthropic media type for supported formats.
_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Claude Vision downscales anything larger than ~1568px on the long edge server-
# side. Doing it ourselves with a high-quality filter keeps small receipt text
# crisp instead of aliased, and shrinks the payload we base64-encode.
_MAX_EDGE = 1568


def is_supported(filetype: str) -> bool:
    """True if the Slack `filetype` is one Claude Vision accepts directly."""
    return filetype.lower() in SUPPORTED_FILETYPES


def media_type_for(filetype: str) -> str:
    """Anthropic media type for a supported filetype. Converted images are always
    PNG, so callers pass 'png' after conversion."""
    return _MEDIA_TYPES[filetype.lower()]


def sha256_hash(image_bytes: bytes) -> str:
    """SHA-256 hex digest of the given bytes (used for the UNIQUE dedup key)."""
    return hashlib.sha256(image_bytes).hexdigest()


def _normalize(img: Image.Image) -> Image.Image:
    """Upright, right-size, and contrast-even a photo so faint receipt text reads.

    Steps, in order:
      1. Bake EXIF orientation into the pixels — phone cameras record rotation in a
         tag Claude Vision ignores, so an unbaked portrait shot reaches the model
         sideways/upside-down. This alone is what most often breaks an unscanned
         picture.
      2. Flatten to a PNG-encodable mode (CMYK/P/etc. -> RGB).
      3. Downscale to _MAX_EDGE on the long side with Lanczos, so small print stays
         legible instead of being aliased by the server-side resize.
      4. Mild auto-contrast (ignoring the brightest/darkest 1% so one glare spec or
         a dark background doesn't blow out the rest) — rescues faint thermal-paper
         text and evens out the uneven lighting a flatbed scan never has.
    """
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    long_edge = max(img.size)
    if long_edge > _MAX_EDGE:
        scale = _MAX_EDGE / long_edge
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(img, cutoff=1)


def to_png(image_bytes: bytes) -> bytes:
    """Normalize an image and return PNG bytes via Pillow.

    Raises PIL.UnidentifiedImageError (a subclass of OSError) if the bytes are not
    a decodable image, so the caller can fall back to manual entry / a clear error.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        out = io.BytesIO()
        _normalize(img).save(out, format="PNG")
        return out.getvalue()


def encode_base64(image_bytes: bytes) -> str:
    """Base64-encode image bytes as an ASCII string for the Claude Vision API."""
    return base64.standard_b64encode(image_bytes).decode("ascii")


def prepare_for_vision(image_bytes: bytes) -> tuple[bytes, str]:
    """Return (normalized PNG bytes, media_type) ready for Claude Vision.

    Every image — supported format or not — is normalized (upright, right-sized,
    contrast-evened) and re-encoded to PNG. A raw phone photo in a "supported"
    format is exactly the case that reads badly untouched, so there is no
    pass-through fast path; the small re-encode cost buys a large accuracy gain on
    unscanned pictures. Raises PIL.UnidentifiedImageError (a subclass of OSError)
    if the bytes are not a decodable image.
    """
    return to_png(image_bytes), media_type_for("png")
