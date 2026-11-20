"""Multimodal offline vision and image reasoning engine for JUGNU.

Provides:
- Image validation, format checks, and aspect-ratio preserving dimension capping using PIL.
- Base64 encoding pipeline for Ollama multimodal API endpoints (/api/generate and /api/chat).
- Multimodal vision model detection (LLaVA, Moondream, LLaMA-Vision, BakLLaVA, MiniCPM-V).
- Graceful compatibility checking and text-only model fallback advisories.
"""
from __future__ import annotations

import base64
import io
import os
from typing import BinaryIO, List, Optional, Tuple, Union

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False


# Supported standard image extensions
SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

SUPPORTED_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Substrings identifying multimodal vision-capable models in Ollama
VISION_MODEL_SUBSTRINGS = (
    "llava",
    "moondream",
    "vision",
    "bakllava",
    "minicpm",
    "qwen-vl",
    "deepseek-vl",
)

DEFAULT_MAX_DIMENSION = 1024
DEFAULT_JPEG_QUALITY = 85


def is_vision_model(model_name: Optional[str]) -> bool:
    """Return True if the model name indicates multimodal / vision reasoning capability."""
    if not model_name or not isinstance(model_name, str):
        return False
    normalized = model_name.strip().lower()
    return any(sub in normalized for sub in VISION_MODEL_SUBSTRINGS)


def validate_image_path(path: Union[str, os.PathLike]) -> bool:
    """Check if the path exists, is a file, and has a supported image extension."""
    if not path:
        return False
    path_str = str(path).strip()
    if not os.path.isfile(path_str):
        return False
    _, ext = os.path.splitext(path_str.lower())
    return ext in SUPPORTED_IMAGE_EXTENSIONS


def encode_image_bytes(image_bytes: bytes) -> str:
    """Encode raw image bytes into a standard Base64 ASCII string."""
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise TypeError("image_bytes must be bytes or bytearray")
    return base64.b64encode(image_bytes).decode("ascii")


def decode_image_base64(b64_str: str) -> bytes:
    """Decode a Base64 string back into raw image bytes."""
    if not isinstance(b64_str, str):
        raise TypeError("b64_str must be a string")
    clean_str = b64_str.strip()
    if clean_str.startswith("data:") and ";base64," in clean_str:
        clean_str = clean_str.split(";base64,")[1]
    return base64.b64decode(clean_str)


def process_and_encode_image(
    image_input: Union[str, bytes, BinaryIO, Image.Image],
    max_dim: int = DEFAULT_MAX_DIMENSION,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Tuple[str, Tuple[int, int]]:
    """Process an image, scale dimensions down to `max_dim` if larger, and return (base64_str, (w, h)).

    Args:
        image_input: File path, raw bytes, file-like buffer, or PIL.Image instance.
        max_dim: Maximum allowed width or height in pixels.
        quality: JPEG compression quality if re-encoding.

    Returns:
        Tuple of (base64_encoded_string, (final_width, final_height)).

    Raises:
        ValueError: If input format is invalid or unsupported.
        RuntimeError: If PIL is not installed.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow (PIL) is required for image processing. Please install pillow.")

    img = None
    if isinstance(image_input, Image.Image):
        img = image_input.copy()
    elif isinstance(image_input, (str, os.PathLike)):
        path_str = str(image_input).strip()
        if not os.path.isfile(path_str):
            raise FileNotFoundError(f"Image file not found: {path_str}")
        _, ext = os.path.splitext(path_str.lower())
        if ext and ext not in SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(
                f"Unsupported image extension '{ext}'. Supported formats: {', '.join(SUPPORTED_IMAGE_EXTENSIONS)}"
            )
        img = Image.open(path_str)
    elif isinstance(image_input, (bytes, bytearray)):
        img = Image.open(io.BytesIO(image_input))
    elif hasattr(image_input, "read"):
        img = Image.open(image_input)
    else:
        raise TypeError(f"Unsupported image_input type: {type(image_input)}")

    try:
        # Verify valid image dimensions
        orig_w, orig_h = img.size
        if orig_w <= 0 or orig_h <= 0:
            raise ValueError(f"Invalid image dimensions: {orig_w}x{orig_h}")

        # Scale down if either dimension exceeds max_dim, preserving aspect ratio
        w, h = orig_w, orig_h
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            w = max(1, int(round(w * scale)))
            h = max(1, int(round(h * scale)))
            resample_filter = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((w, h), resample=resample_filter)

        # Encode to bytes buffer
        out_buf = io.BytesIO()
        img_format = img.format if img.format in ("PNG", "JPEG", "WEBP") else None

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            # Preserve transparency if PNG/WEBP
            save_format = img_format if img_format in ("PNG", "WEBP") else "PNG"
            img.save(out_buf, format=save_format)
        else:
            # Convert to RGB for clean standard JPEG
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(out_buf, format="JPEG", quality=quality, optimize=True)

        final_bytes = out_buf.getvalue()
        b64_str = encode_image_bytes(final_bytes)
        return b64_str, (w, h)
    finally:
        if isinstance(image_input, (str, os.PathLike)) and img:
            img.close()


def check_vision_compatibility(model_name: Optional[str], has_images: bool) -> Optional[str]:
    """Return an advisory message if images are provided to a non-vision model, else None."""
    if not has_images:
        return None
    if not is_vision_model(model_name):
        model_display = model_name or "current"
        return (
            f"⚠️ Model '{model_display}' is a text-only model and cannot process visual images. "
            f"Please switch to a multimodal model (such as 'llava', 'moondream', or 'llama3.2-vision') "
            f"to analyze this image."
        )
    return None


def format_multimodal_payload(
    prompt: str,
    images: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
) -> dict:
    """Format payload dictionary with prompt, optional base64 images array, and system prompt."""
    payload = {
        "prompt": prompt,
    }
    if images:
        clean_images = [img.strip() for img in images if isinstance(img, str) and img.strip()]
        if clean_images:
            payload["images"] = clean_images
    if system_prompt:
        payload["system"] = system_prompt
    return payload
