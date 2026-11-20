"""Comprehensive automated tests for Multimodal Vision & Image Reasoning Engine (#86)."""
import base64
import io
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import assistant
import core
import jugnu_core
from multimodal import (
    DEFAULT_MAX_DIMENSION,
    SUPPORTED_IMAGE_EXTENSIONS,
    check_vision_compatibility,
    decode_image_base64,
    encode_image_bytes,
    format_multimodal_payload,
    is_vision_model,
    process_and_encode_image,
    validate_image_path,
)


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_pil_image():
    """Create a sample 100x100 RGB PIL image."""
    return Image.new("RGB", (100, 100), color=(255, 0, 0))


@pytest.fixture
def large_pil_image():
    """Create a sample 2000x1000 RGB PIL image."""
    return Image.new("RGB", (2000, 1000), color=(0, 255, 0))


@pytest.fixture
def sample_image_file(tmp_path, sample_pil_image):
    """Save a sample PNG image to a temp file and return its path."""
    file_path = tmp_path / "test_sample.png"
    sample_pil_image.save(file_path, format="PNG")
    return str(file_path)


@pytest.fixture
def sample_jpeg_file(tmp_path, sample_pil_image):
    """Save a sample JPEG image to a temp file and return its path."""
    file_path = tmp_path / "test_sample.jpg"
    sample_pil_image.save(file_path, format="JPEG")
    return str(file_path)


# ---------------------------------------------------------------------------
# 1. Vision Model Capability Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("llava", True),
        ("llava:13b", True),
        ("llava:latest", True),
        ("moondream", True),
        ("moondream:1.8b", True),
        ("llama3.2-vision", True),
        ("llama3.2-vision:11b", True),
        ("bakllava", True),
        ("minicpm-v", True),
        ("qwen-vl", True),
        ("deepseek-vl", True),
        ("llama3.2", False),
        ("llama3:8b", False),
        ("mistral", False),
        ("deepseek-coder", False),
        ("qwen2.5:7b", False),
        ("phi3:mini", False),
        ("", False),
        (None, False),
    ],
)
def test_is_vision_model(model_name, expected):
    """Verify is_vision_model accurately identifies multimodal vision models."""
    assert is_vision_model(model_name) is expected


# ---------------------------------------------------------------------------
# 2. Image Path & Format Validation
# ---------------------------------------------------------------------------

def test_validate_image_path_valid_formats(tmp_path):
    """Verify valid image extensions are accepted."""
    for ext in SUPPORTED_IMAGE_EXTENSIONS:
        test_file = tmp_path / f"test{ext}"
        test_file.write_bytes(b"dummy image bytes")
        assert validate_image_path(str(test_file)) is True


def test_validate_image_path_invalid_formats(tmp_path):
    """Verify unsupported extensions or non-existent files return False."""
    assert validate_image_path(str(tmp_path / "missing.png")) is False
    assert validate_image_path(None) is False
    assert validate_image_path("") is False

    txt_file = tmp_path / "test.txt"
    txt_file.write_text("not an image")
    assert validate_image_path(str(txt_file)) is False

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4")
    assert validate_image_path(str(pdf_file)) is False


# ---------------------------------------------------------------------------
# 3. Base64 Encoding and Decoding
# ---------------------------------------------------------------------------

def test_base64_roundtrip():
    """Verify raw bytes can be encoded to base64 and decoded back identically."""
    raw = b"JUGNU_OFFLINE_VISION_TEST_BYTES_12345"
    encoded = encode_image_bytes(raw)
    assert isinstance(encoded, str)
    decoded = decode_image_base64(encoded)
    assert decoded == raw


def test_decode_image_base64_with_data_uri():
    """Verify decode_image_base64 handles data:image/png;base64, prefixes."""
    raw = b"sample_pixels"
    encoded = "data:image/png;base64," + encode_image_bytes(raw)
    decoded = decode_image_base64(encoded)
    assert decoded == raw


# ---------------------------------------------------------------------------
# 4. Image Processing, Resizing, & Dimension Capping
# ---------------------------------------------------------------------------

def test_process_and_encode_image_preserves_small_dimensions(sample_pil_image):
    """Images smaller than max_dim should retain their exact dimensions."""
    b64_str, (w, h) = process_and_encode_image(sample_pil_image, max_dim=1024)
    assert w == 100
    assert h == 100
    assert isinstance(b64_str, str)
    assert len(b64_str) > 0

    # Verify decoded image opens and has correct dimensions
    decoded_bytes = decode_image_base64(b64_str)
    reloaded = Image.open(io.BytesIO(decoded_bytes))
    assert reloaded.size == (100, 100)


def test_process_and_encode_image_caps_large_dimensions(large_pil_image):
    """Images exceeding max_dim should be scaled down while preserving aspect ratio."""
    # 2000x1000 scaled to max 1024 -> should become 1024x512
    b64_str, (w, h) = process_and_encode_image(large_pil_image, max_dim=1024)
    assert w == 1024
    assert h == 512

    decoded_bytes = decode_image_base64(b64_str)
    reloaded = Image.open(io.BytesIO(decoded_bytes))
    assert reloaded.size == (1024, 512)


def test_process_and_encode_image_from_file_path(sample_image_file):
    """Verify processing directly from a valid file path."""
    b64_str, (w, h) = process_and_encode_image(sample_image_file)
    assert w == 100
    assert h == 100
    assert len(b64_str) > 0


def test_process_and_encode_image_missing_file_raises():
    """Verify FileNotFoundError is raised for non-existent image paths."""
    with pytest.raises(FileNotFoundError):
        process_and_encode_image("non_existent_image_path_12345.png")


def test_process_and_encode_image_unsupported_extension(tmp_path):
    """Verify ValueError is raised for unsupported file formats."""
    bad_file = tmp_path / "bad.bmp"
    bad_file.write_bytes(b"dummy")
    with pytest.raises(ValueError, match="Unsupported image extension"):
        process_and_encode_image(str(bad_file))


# ---------------------------------------------------------------------------
# 5. Vision Model Compatibility & Fallback Advisories
# ---------------------------------------------------------------------------

def test_check_vision_compatibility_no_images():
    """When no images are provided, compatibility check returns None."""
    assert check_vision_compatibility("llama3.2", has_images=False) is None
    assert check_vision_compatibility("llava", has_images=False) is None


def test_check_vision_compatibility_with_vision_model():
    """When images are provided to a vision model, check passes (returns None)."""
    assert check_vision_compatibility("llava:13b", has_images=True) is None
    assert check_vision_compatibility("moondream", has_images=True) is None
    assert check_vision_compatibility("llama3.2-vision", has_images=True) is None


def test_check_vision_compatibility_with_text_only_model():
    """When images are provided to a text-only model, returns user advisory."""
    advisory = check_vision_compatibility("llama3.2", has_images=True)
    assert advisory is not None
    assert "text-only" in advisory
    assert "llama3.2" in advisory
    assert "llava" in advisory


# ---------------------------------------------------------------------------
# 6. Multimodal Payload Construction
# ---------------------------------------------------------------------------

def test_format_multimodal_payload():
    """Verify payload dictionary matches Ollama expectations."""
    payload = format_multimodal_payload(
        prompt="Explain this diagram",
        images=["b64_image_data_1", "b64_image_data_2"],
        system_prompt="You are an expert analyst.",
    )
    assert payload["prompt"] == "Explain this diagram"
    assert payload["images"] == ["b64_image_data_1", "b64_image_data_2"]
    assert payload["system"] == "You are an expert analyst."


# ---------------------------------------------------------------------------
# 7. Core & Assistant Multimodal Integration
# ---------------------------------------------------------------------------

def test_build_messages_preserves_images():
    """Verify core.build_messages preserves the images field on user messages."""
    history = [
        {"role": "user", "content": "Look at this", "images": ["fake_b64_123"]},
        {"role": "assistant", "content": "I see it."},
        {"role": "user", "content": "What is it?"},
    ]
    messages = core.build_messages("You are helpful", history)
    user_msg_with_img = messages[1]
    assert user_msg_with_img["role"] == "user"
    assert user_msg_with_img["images"] == ["fake_b64_123"]


def test_assistant_ask_ai_with_text_only_model_returns_advisory(sample_image_file):
    """When ask_ai is invoked with an image and a text-only model, it returns advisory."""
    reply = assistant.ask_ai(
        "Analyze this image",
        model="llama3.2:3b",
        images=[sample_image_file],
    )
    assert "text-only" in reply
    assert "multimodal model" in reply


def test_assistant_ask_ai_with_vision_model_sends_images(sample_image_file):
    """When ask_ai is invoked with a vision model, it formats payload with images."""
    mock_result = core.ChatResult(answer="This is a red square.")
    with patch("assistant.chat", return_value=mock_result) as mock_chat:
        reply = assistant.ask_ai(
            "What color is this?",
            model="llava:7b",
            images=[sample_image_file],
        )
        assert reply == "This is a red square."
        assert mock_chat.called
        sent_messages = mock_chat.call_args[0][0]
        user_msg = next(m for m in sent_messages if m["role"] == "user")
        assert "images" in user_msg
        assert len(user_msg["images"]) == 1
        assert len(user_msg["images"][0]) > 0


def test_jugnu_core_ask_ollama_multimodal_fallback():
    """Verify jugnu_core.ask_ollama returns advisory if chat has images for text-only model."""
    chat = {
        "id": "chat-vision-1",
        "model": "llama3.2",
        "messages": [
            {"role": "user", "content": "Examine this", "images": ["sample_b64"]},
        ],
    }
    answer, is_error = jugnu_core.ask_ollama(chat, "en", model="llama3.2")
    assert is_error is True
    assert "text-only" in answer
    assert "llava" in answer


def test_jugnu_core_ask_ollama_multimodal_success():
    """Verify jugnu_core.ask_ollama sends images array to Ollama for vision model."""
    chat = {
        "id": "chat-vision-2",
        "model": "llava",
        "messages": [
            {"role": "user", "content": "Examine this", "images": ["sample_b64"]},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "message": {"content": "Detected a red rectangle in the image."},
        "eval_count": 12,
        "eval_duration": 100000000,
    }
    mock_resp.raise_for_status.return_value = None

    with patch("requests.post", return_value=mock_resp) as mock_post:
        answer, is_error = jugnu_core.ask_ollama(chat, "en", model="llava")
        assert is_error is False
        assert "red rectangle" in answer
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "llava"
        user_msg = next(m for m in payload["messages"] if m["role"] == "user")
        assert user_msg["images"] == ["sample_b64"]



# ---------------------------------------------------------------------------
# 8. Desktop App UI Multimodal Attachment & Chip Rendering Tests
# ---------------------------------------------------------------------------

def test_desktop_pick_and_remove_image_attachment(sample_image_file, qtbot):
    """Verify JugnuWindow handles image attachment via file picker and renders/removes chips."""
    import desktop_app
    with patch.object(desktop_app.JugnuWindow, "warm_up", lambda self: None):
        win = desktop_app.JugnuWindow()
        qtbot.addWidget(win)

        assert len(win.attached_images) == 0
        assert win.attachments_container.isHidden() is True

        # Mock QFileDialog.getOpenFileNames to return sample image
        with patch("desktop_app.QFileDialog.getOpenFileNames", return_value=([sample_image_file], "")):
            win.pick_image_attachments()

        assert len(win.attached_images) == 1
        assert win.attached_images[0]["name"] == "test_sample.png"
        assert len(win.attached_images[0]["base64"]) > 0
        assert win.attachments_container.isHidden() is False

        # Remove the attachment
        win.remove_image_attachment(0)
        assert len(win.attached_images) == 0
        assert win.attachments_container.isHidden() is True


def test_desktop_send_message_attaches_images(sample_image_file, qtbot):
    """Verify sending a message with an attached image embeds images array in chat messages."""
    import desktop_app
    with patch.object(desktop_app.JugnuWindow, "warm_up", lambda self: None):
        win = desktop_app.JugnuWindow()
        qtbot.addWidget(win)

        # Manually attach an image
        win.attached_images.append({
            "name": "photo.png",
            "path": sample_image_file,
            "base64": "dummy_b64_string_xyz",
            "width": 100,
            "height": 100,
        })
        win.render_attachment_chips()
        assert win.attachments_container.isHidden() is False

        win.input_box.setPlainText("Describe this image")
        with patch.object(win, "ask_ollama") as mock_ask:
            win.send_message()
            assert mock_ask.called

        # Verify chat history contains images
        chat = win.current_chat()
        last_msg = chat["messages"][-1]
        assert last_msg["role"] == "user"
        assert last_msg["content"] == "Describe this image"
        assert last_msg["images"] == ["dummy_b64_string_xyz"]

        # Attached images should be cleared after send
        assert len(win.attached_images) == 0
        assert win.attachments_container.isHidden() is True
