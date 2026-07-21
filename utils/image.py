"""Image utilities: filetype check, Pillow conversion, SHA-256 hashing, base64.

Claude Vision accepts JPEG, PNG, GIF, and WebP. Anything else (HEIC, TIFF, BMP,
…) is converted to PNG via Pillow before it reaches the model. Dedup hashing is
done by the caller on the *final* bytes it will send to Vision, matching the
pipeline order in ARCHITECTURE.md (convert → hash).
"""

import base64
import hashlib
import io

from PIL import Image

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


def to_png(image_bytes: bytes) -> bytes:
    """Convert an unsupported image to PNG bytes via Pillow.

    Raises PIL.UnidentifiedImageError (a subclass of OSError) if the bytes are not
    a decodable image, so the caller can fall back to manual entry / a clear
    error. CMYK/other modes are flattened to RGB, which PNG can always encode.
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def encode_base64(image_bytes: bytes) -> str:
    """Base64-encode image bytes as an ASCII string for the Claude Vision API."""
    return base64.standard_b64encode(image_bytes).decode("ascii")
