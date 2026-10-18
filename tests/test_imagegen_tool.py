"""Unit tests for the generate_image tool (src/nixclaw_agent/tools/imagegen_ops.py).

Covers:
- disabled kill-switch
- channel_id / input_images path traversal rejection
- missing input image
- success: PNG lands in {data_dir}/{channel_id}/attachments/, bare filename returned
- edit mode: input image bytes forwarded to the client
- client error propagation (ImageGenError, ImageGenConcurrencyLimitError)
"""

import re
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.nixclaw_agent import config as config_module
from src.nixclaw_agent.imagegen import ImageGenConcurrencyLimitError, ImageGenError
from src.nixclaw_agent.tools import imagegen_ops


PNG_BYTES = b"\x89PNG-fake"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.config.paths, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.config.image_gen, "enabled", True)
    return config_module.config


@contextmanager
def _mock_client(generate_result=PNG_BYTES, generate_side_effect=None):
    """Patch imagegen_ops.FluxImageClient; yields the client instance mock."""
    instance = MagicMock()
    instance.generate = AsyncMock(
        return_value=generate_result, side_effect=generate_side_effect
    )
    with patch.object(imagegen_ops, "FluxImageClient", return_value=instance):
        yield instance


class TestGenerateImageTool:
    @pytest.mark.asyncio
    async def test_disabled_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module.config.paths, "data_dir", tmp_path)
        monkeypatch.setattr(config_module.config.image_gen, "enabled", False)
        result = await imagegen_ops.generate_image("a red bicycle", channel_id="chan1")
        assert result.success is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_channel_id_traversal_rejected(self, cfg):
        result = await imagegen_ops.generate_image("x", channel_id="../etc")
        assert result.success is False
        assert "Invalid channel_id" in result.error

    @pytest.mark.asyncio
    async def test_input_image_dotdot_rejected(self, cfg):
        result = await imagegen_ops.generate_image(
            "x", input_images=["../evil.png"], channel_id="chan1"
        )
        assert result.success is False
        assert "path traversal" in result.error

    @pytest.mark.asyncio
    async def test_input_image_subdir_rejected(self, cfg):
        result = await imagegen_ops.generate_image(
            "x", input_images=["sub/a.png"], channel_id="chan1"
        )
        assert result.success is False
        assert "path traversal" in result.error

    @pytest.mark.asyncio
    async def test_input_image_not_found(self, cfg):
        (cfg.paths.data_dir / "chan1" / "attachments").mkdir(parents=True)
        result = await imagegen_ops.generate_image(
            "x", input_images=["missing.png"], channel_id="chan1"
        )
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_success_writes_png_and_returns_filename(self, cfg):
        with _mock_client() as client:
            result = await imagegen_ops.generate_image("a red bicycle", channel_id="chan1")
        assert result.success is True
        assert result.error is None
        assert re.fullmatch(r"generated_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[a-z0-9]{4}\.png",
                            result.output)
        filepath = cfg.paths.data_dir / "chan1" / "attachments" / result.output
        assert filepath.exists()
        assert filepath.read_bytes() == PNG_BYTES
        # Client got no input image (pure generation mode).
        _, kwargs = client.generate.call_args
        assert kwargs["input_images"] is None

    @pytest.mark.asyncio
    async def test_edit_mode_forwards_input_bytes(self, cfg):
        att_dir = cfg.paths.data_dir / "chan2" / "attachments"
        att_dir.mkdir(parents=True)
        (att_dir / "photo.jpg").write_bytes(b"JPEGDATA")
        with _mock_client() as client:
            result = await imagegen_ops.generate_image(
                "make it blue", input_images=["photo.jpg"], channel_id="chan2"
            )
        assert result.success is True
        _, kwargs = client.generate.call_args
        assert kwargs["input_images"] == [("photo.jpg", b"JPEGDATA")]

    @pytest.mark.asyncio
    async def test_size_forwarded(self, cfg):
        with _mock_client() as client:
            await imagegen_ops.generate_image("x", size="1536x1024", channel_id="chan1")
        _, kwargs = client.generate.call_args
        assert kwargs["size"] == "1536x1024"

    @pytest.mark.asyncio
    async def test_client_error_propagated_as_failure(self, cfg):
        with _mock_client(generate_side_effect=ImageGenError("boom")):
            result = await imagegen_ops.generate_image("x", channel_id="chan1")
        assert result.success is False
        assert "Image generation failed" in result.error

    @pytest.mark.asyncio
    async def test_concurrency_limit_propagated(self, cfg):
        with _mock_client(generate_side_effect=ImageGenConcurrencyLimitError("timed out")):
            result = await imagegen_ops.generate_image("x", channel_id="chan1")
        assert result.success is False
        assert "timed out" in result.error
