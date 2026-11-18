"""Image normalization, hashing, and encoding."""

import base64
import hashlib
import io

import pytest
from PIL import Image, UnidentifiedImageError

from utils import image as image_utils


def _img_bytes(fmt="JPEG", size=(100, 50), mode="RGB", color=(200, 100, 50), exif=None):
    img = Image.new(mode, size, color if mode == "RGB" else 0)
    buf = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _open(png_bytes):
    return Image.open(io.BytesIO(png_bytes))


# --- hashing / encoding ------------------------------------------------------

def test_sha256_matches_hashlib_and_is_deterministic():
    data = b"receipt-bytes"
    assert image_utils.sha256_hash(data) == hashlib.sha256(data).hexdigest()
    assert image_utils.sha256_hash(data) == image_utils.sha256_hash(data)


def test_sha256_differs_for_different_input():
    assert image_utils.sha256_hash(b"a") != image_utils.sha256_hash(b"b")


def test_encode_base64_round_trips():
    data = b"\x00\x01binary\xff"
    encoded = image_utils.encode_base64(data)
    assert isinstance(encoded, str)
    assert base64.standard_b64decode(encoded) == data


# --- filetype helpers --------------------------------------------------------

@pytest.mark.parametrize("ft", ["jpg", "JPEG", "png", "gif", "webp"])
def test_supported_filetypes(ft):
    assert image_utils.is_supported(ft)


@pytest.mark.parametrize("ft", ["heic", "tiff", "bmp", "pdf"])
def test_unsupported_filetypes(ft):
    assert not image_utils.is_supported(ft)


def test_media_type_for():
    assert image_utils.media_type_for("JPG") == "image/jpeg"
    assert image_utils.media_type_for("png") == "image/png"


# --- prepare_for_vision ------------------------------------------------------

@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"])
def test_every_format_is_normalized_to_png(fmt):
    out, media_type = image_utils.prepare_for_vision(_img_bytes(fmt))
    assert media_type == "image/png"
    assert _open(out).format == "PNG"


def test_heic_is_decodable():
    pillow_heif = pytest.importorskip("pillow_heif")
    img = Image.new("RGB", (64, 32), (10, 20, 30))
    buf = io.BytesIO()
    try:
        img.save(buf, format="HEIF")
    except Exception as e:  # encoder may be unavailable on some builds
        pytest.skip(f"HEIF encoder unavailable: {e}")
    out, _ = image_utils.prepare_for_vision(buf.getvalue())
    assert _open(out).size == (64, 32)


def test_large_image_is_downscaled_preserving_aspect():
    out, _ = image_utils.prepare_for_vision(_img_bytes(size=(4000, 2000)))
    w, h = _open(out).size
    assert max(w, h) == image_utils._MAX_EDGE
    assert abs(w / h - 2.0) < 0.01


def test_small_image_is_not_upscaled():
    out, _ = image_utils.prepare_for_vision(_img_bytes(size=(300, 200)))
    assert _open(out).size == (300, 200)


def test_exif_orientation_is_baked_in():
    # Orientation 6 = "rotate 90° CW to display" — a sideways portrait phone photo.
    exif = Image.Exif()
    exif[0x0112] = 6
    out, _ = image_utils.prepare_for_vision(_img_bytes(size=(200, 100), exif=exif))
    assert _open(out).size == (100, 200)


@pytest.mark.parametrize("mode", ["CMYK", "P", "RGBA", "LA"])
def test_odd_color_modes_are_flattened(mode):
    fmt = "TIFF" if mode == "CMYK" else "PNG"
    out, _ = image_utils.prepare_for_vision(_img_bytes(fmt, mode=mode))
    assert _open(out).mode in ("RGB", "L")


def test_normalization_is_deterministic_so_dedup_works():
    # The dedup hash is taken over the *normalized* bytes; re-uploading the same
    # file must produce the same hash.
    raw = _img_bytes(size=(2500, 1200))
    a, _ = image_utils.prepare_for_vision(raw)
    b, _ = image_utils.prepare_for_vision(raw)
    assert image_utils.sha256_hash(a) == image_utils.sha256_hash(b)


@pytest.mark.parametrize("junk", [b"", b"not an image", b"%PDF-1.4\n..."])
def test_undecodable_bytes_raise_oserror(junk):
    with pytest.raises(OSError):
        image_utils.prepare_for_vision(junk)


def test_unidentified_image_error_is_oserror():
    # file_upload relies on catching a generic Exception, but the docstring
    # promises an OSError subclass — keep that contract honest.
    assert issubclass(UnidentifiedImageError, OSError)
