"""MIME magic-byte validation for downloaded attachments.

Traces to:
- seed-spec.md §2.4 (attachments: image types + size cap)
- security-model.md §6 SEC-P0-19 (MIME sniff first 16 bytes)
- claude-rules.md MUST "Attachment security"

Validates the declared file extension against the actual byte signature so
a malicious attachment with `.png` extension but PE / ELF / ZIP / script
content cannot sneak onto disk. On mismatch the caller discards the bytes
and records only the CDN URL.
"""

from __future__ import annotations

from typing import Final

# Well-known magic bytes for images Stage 2 allows (seed-spec §2.4 default).
# Each entry is (extension, signature_prefix_or_callable).
# Callable signatures handle cases like WebP where the magic is not a flat prefix.
_PNG: Final[bytes] = b"\x89PNG\r\n\x1a\n"  # 8-byte PNG signature
_JPEG_PREFIX: Final[bytes] = b"\xff\xd8\xff"  # 3-byte JFIF/Exif prefix
_GIF_87A: Final[bytes] = b"GIF87a"
_GIF_89A: Final[bytes] = b"GIF89a"
# WebP: `RIFF????WEBP` — 12 bytes with the 4-byte length in the middle.
_WEBP_RIFF: Final[bytes] = b"RIFF"
_WEBP_TAG: Final[bytes] = b"WEBP"


def detect_image_type(first_16_bytes: bytes) -> str | None:
    """Return a canonical lowercase extension (`.png`, `.jpg`, `.webp`, `.gif`)
    if `first_16_bytes` starts with a known image signature; else `None`.

    The caller passes exactly the first 16 bytes of the stream (SEC-P0-19).
    """
    if not isinstance(first_16_bytes, bytes) or len(first_16_bytes) < 3:
        return None
    head = first_16_bytes
    if head.startswith(_PNG):
        return ".png"
    if head.startswith(_JPEG_PREFIX):
        return ".jpg"
    if head.startswith(_GIF_87A) or head.startswith(_GIF_89A):
        return ".gif"
    # WebP: 12-byte sniff, needs at least len 12.
    if len(head) >= 12 and head[0:4] == _WEBP_RIFF and head[8:12] == _WEBP_TAG:
        return ".webp"
    return None


def declared_extension_matches(declared_filename: str, sniffed_ext: str | None) -> bool:
    """True when the sniffed extension matches the declared filename suffix
    (case-insensitive). Treats `.jpeg` and `.jpg` as equivalent."""
    if sniffed_ext is None:
        return False
    declared = _normalize_suffix(declared_filename)
    sniffed = _normalize_suffix(sniffed_ext)
    return declared == sniffed


def _normalize_suffix(name: str) -> str:
    suffix = ""
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[1].lower()
    if suffix == ".jpeg":
        return ".jpg"
    return suffix
