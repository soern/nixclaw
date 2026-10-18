"""Image generation tool backed by the FLUX.2 [klein] sidecar server.

One deliberately minimal tool: generate a new image from a prompt, or edit
an existing image from the channel's attachments/ directory. The main agent
composes the prompt itself; this tool does not rewrite it.

On success the PNG is written to the channel's attachments/ directory and
only the bare filename is returned, so the LLM can pass it straight to
send_signal_attachment.
"""

import logging
import random
from datetime import datetime
from typing import Optional

from ..config import config, normalize_image_steps
from ..imagegen import FluxImageClient, ImageGenConcurrencyLimitError, ImageGenError
from ..types import ToolResult
from .paths import get_attachments_dir

LOG = logging.getLogger(__name__)

_TOOL_NAME = "generate_image"


def _error(msg: str) -> ToolResult:
    return ToolResult(name=_TOOL_NAME, success=False, output=None, error=msg)


async def generate_image(
    prompt: str,
    input_images: Optional[list] = None,
    size: Optional[str] = None,
    steps: Optional[int] = None,
    channel_id: str = "",
) -> ToolResult:
    """Generate or edit an image and save the PNG to the channel's attachments/.

    Args:
        prompt: Rich, detailed description of the desired image (or edit).
        input_images: Optional list of filenames in the attachments/ directory
            to use as reference images for an edit. A bare string is accepted
            for LLM robustness.
        size: Optional "WxH" (both dims 16-multiples, max 2048).
        steps: Optional number of sampling passes (1 to image_gen.max_steps).
            None uses the configured default. Only use when the user
            explicitly asks for better quality / more detail - never for
            upscaling alone.
        channel_id: The channel ID to scope file operations to (injected).

    Returns:
        ToolResult whose output is the bare filename on success.
    """
    try:
        if not config.image_gen.enabled:
            return _error("Image generation is disabled (set image_gen.enabled: true in config/agent.yaml)")

        # Accept both list and bare string (LLM robustness)
        if isinstance(input_images, str):
            input_images = [input_images]

        # Normalize optional steps parameter
        clamped = False
        if steps is not None:
            try:
                steps, clamped = normalize_image_steps(steps, config.image_gen.max_steps)
            except ValueError:
                return _error(f"Invalid steps value: {steps!r} - must be a positive integer")

        try:
            attachments_dir = get_attachments_dir(channel_id)
        except ValueError:
            return _error(f"Invalid channel_id: {channel_id!r}")

        image_files: list[tuple[str, bytes]] = []
        if input_images:
            if len(input_images) > config.image_gen.max_input_images:
                return _error(f"Too many input images ({len(input_images)}). Maximum is {config.image_gen.max_input_images}.")
            for name in input_images:
                if ".." in name or "/" in name:
                    return _error(f"Invalid input image - path traversal not allowed: {name}")
                src_path = (attachments_dir / name).resolve()
                if src_path.parent != attachments_dir:
                    return _error(f"Invalid input image - path traversal not allowed: {name}")
                if not src_path.exists():
                    return _error(f"Input image not found: {name}")
                image_files.append((name, src_path.read_bytes()))

        client = FluxImageClient(config.image_gen)
        try:
            generated = await client.generate(
                prompt,
                input_images=image_files if image_files else None,
                size=size,
                steps=steps,
            )
        except ImageGenConcurrencyLimitError as e:
            return _error(str(e))
        except ImageGenError as e:
            return _error(f"Image generation failed: {e}")

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        random_suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
        filename = f"generated_{timestamp}_{random_suffix}.png"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        filepath = (attachments_dir / filename).resolve()
        if filepath.parent != attachments_dir:
            return _error("Invalid filename: must be a plain filename without directory parts")
        filepath.write_bytes(generated)

        output = f"{filepath.name} (steps clamped to {config.image_gen.max_steps})" if clamped else filepath.name
        return ToolResult(name=_TOOL_NAME, success=True, output=output, error=None)
    except Exception as e:
        LOG.error(f"generate_image failed: {e}")
        return _error(str(e))
