"""File operation tools for nixclaw-agent.

These tools provide sandboxed file read/write/search operations
restricted to the context directory for each channel.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import prompts
from ..types import Attachment, ToolResult
from ..config import config
from ..path_safety import is_within_directory
from .paths import get_context_dir

LOG = logging.getLogger(__name__)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


def _get_attachments_dir(channel_id: str) -> Path:
    """Get the attachments directory path for a channel, ensuring it's sandboxed."""
    context_dir = _get_context_dir(channel_id)
    attachments_dir = (context_dir / "attachments").resolve()
    if not is_within_directory(attachments_dir, context_dir):
        raise ValueError("Path traversal detected")
    return attachments_dir


def _is_image_file(filename: str) -> bool:
    """Check if filename has an image extension."""
    ext = Path(filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


def create_image_cache(attachment: Attachment, channel_id: str) -> ToolResult:
    """Create initial .image.md cache file for an attachment.

    Args:
        attachment: The Attachment object with id, content_type, filename, local_path, size
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with success status or error
    """
    try:
        attachments_dir = _get_attachments_dir(channel_id)

        if attachment.filename:
            safe_filename = attachment.filename
        else:
            ext = attachment.content_type.split("/")[-1] if "/" in attachment.content_type else "bin"
            safe_filename = f"{attachment.id}.{ext}"

        if not _is_image_file(safe_filename):
            return ToolResult(
                name="create_image_cache",
                success=False,
                output=None,
                error=f"File is not an image: {safe_filename}",
            )

        cache_path = attachments_dir / f"{safe_filename}.image.md"
        if cache_path.exists():
            return ToolResult(
                name="create_image_cache",
                success=True,
                output=f"Cache already exists: {cache_path.name}",
                error=None,
            )

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"""# Image Analysis: {safe_filename}

## Metadata
- **Stored Path:** attachments/{safe_filename}
- **Received:** {timestamp}
- **Content-Type:** {attachment.content_type}
- **Size:** {attachment.size} bytes

## Analysis History
"""
        cache_path.write_text(content)
        return ToolResult(
            name="create_image_cache",
            success=True,
            output=f"Created image cache: {cache_path.name}",
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="create_image_cache",
            success=False,
            output=None,
            error=str(e),
        )


def append_image_analysis(
    image_filename: str,
    channel_id: str,
    query: str,
    analysis: str
) -> ToolResult:
    """Append a new analysis to the image cache.

    Original analysis is NEVER overwritten - new analyses are appended.

    Args:
        image_filename: The filename of the image in attachments/
        channel_id: The channel ID to scope file operations to
        query: The user's question/query about the image
        analysis: The LLM's analysis result

    Returns:
        ToolResult with success status or error
    """
    try:
        attachments_dir = _get_attachments_dir(channel_id)
        cache_path = attachments_dir / f"{image_filename}.image.md"

        if not _is_image_file(image_filename):
            return ToolResult(
                name="append_image_analysis",
                success=False,
                output=None,
                error=f"File is not an image: {image_filename}",
            )

        target_image = attachments_dir / image_filename
        if not target_image.exists():
            return ToolResult(
                name="append_image_analysis",
                success=False,
                output=None,
                error=f"Image file not found: {image_filename}",
            )

        if not cache_path.exists():
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            size = target_image.stat().st_size if target_image.exists() else 0
            content_type = "image/" + Path(image_filename).suffix.lower().lstrip(".")
            initial_content = f"""# Image Analysis: {image_filename}

## Metadata
- **Stored Path:** attachments/{image_filename}
- **Received:** {timestamp}
- **Content-Type:** {content_type}
- **Size:** {size} bytes

## Analysis History
"""
            cache_path.write_text(initial_content)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        analysis_entry = f"""
### Analysis {len(_get_analysis_entries(cache_path)) + 1}
**Timestamp:** {timestamp}
**User Query:** {query}

{analysis}
"""
        existing_content = cache_path.read_text()
        if "## Analysis History" in existing_content:
            parts = existing_content.split("## Analysis History")
            new_content = parts[0] + "## Analysis History" + analysis_entry + "\n"
            if len(parts) > 1 and parts[1].strip():
                new_content += parts[1]
        else:
            new_content = existing_content + "\n" + analysis_entry

        cache_path.write_text(new_content)
        return ToolResult(
            name="append_image_analysis",
            success=True,
            output=f"Appended analysis to {cache_path.name}",
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="append_image_analysis",
            success=False,
            output=None,
            error=str(e),
        )


def _get_analysis_entries(cache_path: Path) -> list[str]:
    """Get list of analysis entry headers from cache file."""
    if not cache_path.exists():
        return []
    content = cache_path.read_text()
    entries = []
    for line in content.split("\n"):
        if line.startswith("### Analysis"):
            entries.append(line)
    return entries


def get_cached_image_analysis(
    image_filename: str,
    channel_id: str,
    query: str
) -> ToolResult:
    """Check if cache contains relevant analysis for the query.

    Searches the Analysis History section for entries matching the query.
    This is a simple text search, not an LLM call.

    Args:
        image_filename: The filename of the image in attachments/
        channel_id: The channel ID to scope file operations to
        query: The user's question/query about the image

    Returns:
        ToolResult with cached analysis if found, or indication to re-analyze
    """
    try:
        attachments_dir = _get_attachments_dir(channel_id)
        cache_path = attachments_dir / f"{image_filename}.image.md"

        if not cache_path.exists():
            return ToolResult(
                name="get_cached_image_analysis",
                success=True,
                output="NO_CACHE",
                error=None,
            )

        if not _is_image_file(image_filename):
            return ToolResult(
                name="get_cached_image_analysis",
                success=False,
                output=None,
                error=f"File is not an image: {image_filename}",
            )

        content = cache_path.read_text()
        query_lower = query.lower()

        lines = content.split("\n")
        in_analysis = False
        current_entry_lines: list[str] = []
        analysis_entries: list[str] = []

        for line in lines:
            if line.startswith("### Analysis"):
                if current_entry_lines:
                    analysis_entries.append("\n".join(current_entry_lines))
                current_entry_lines = [line]
                in_analysis = True
            elif in_analysis:
                if line.startswith("---") or (line.startswith("#") and not line.startswith("##")):
                    if current_entry_lines:
                        analysis_entries.append("\n".join(current_entry_lines))
                    current_entry_lines = []
                    in_analysis = False
                else:
                    current_entry_lines.append(line)

        if current_entry_lines:
            analysis_entries.append("\n".join(current_entry_lines))

        for entry in analysis_entries:
            entry_lower = entry.lower()
            if query_lower in entry_lower:
                parts = entry.split("**User Query:**")
                if len(parts) > 1:
                    user_query_part = parts[1].split("**Timestamp:**")[0].strip().lower()
                    if user_query_part == query_lower or query_lower in user_query_part:
                        analysis_text = entry.split("**Timestamp:**", 1)[1] if "**Timestamp:**" in entry else entry
                        if "**User Query:**" in analysis_text:
                            analysis_text = analysis_text.split("**User Query:**", 1)[1]
                            if "**Timestamp:**" in analysis_text:
                                analysis_text = analysis_text.split("**Timestamp:**", 1)[1]
                        return ToolResult(
                            name="get_cached_image_analysis",
                            success=True,
                            output=f"CACHE_HIT:{entry}",
                            error=None,
                        )

        return ToolResult(
            name="get_cached_image_analysis",
            success=True,
            output="CACHE_MISS",
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="get_cached_image_analysis",
            success=False,
            output=None,
            error=str(e),
        )


def _get_context_dir(channel_id: str) -> Path:
    """Get the context directory path for a channel, ensuring it's sandboxed."""
    return get_context_dir(channel_id)


def file_read(path: str, channel_id: str) -> ToolResult:
    """Read a file within the context directory (sandboxed).

    Args:
        path: Relative path within the context directory
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with file contents or error
    """
    try:
        context_dir = _get_context_dir(channel_id)
        target_path = (context_dir / path).resolve()

        if not is_within_directory(target_path, context_dir):
            return ToolResult(
                name="file_read",
                success=False,
                output=None,
                error="Only files within the context directory can be accessed",
            )

        if not target_path.exists():
            return ToolResult(
                name="file_read",
                success=False,
                output=None,
                error=f"File not found: {path}",
            )

        if not target_path.is_file():
            return ToolResult(
                name="file_read",
                success=False,
                output=None,
                error=f"Not a file: {path}",
            )

        content = target_path.read_text()
        return ToolResult(
            name="file_read",
            success=True,
            output=content,
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="file_read",
            success=False,
            output=None,
            error=str(e),
        )


def file_write(path: str, content: str, channel_id: str) -> ToolResult:
    """Write content to a file within the context directory (sandboxed).

    Args:
        path: Relative path within the context directory
        content: Content to write to the file
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with success status or error
    """
    try:
        context_dir = _get_context_dir(channel_id)
        target_path = (context_dir / path).resolve()

        if not is_within_directory(target_path, context_dir):
            return ToolResult(
                name="file_write",
                success=False,
                output=None,
                error="Only files within the context directory can be accessed",
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content)

        return ToolResult(
            name="file_write",
            success=True,
            output=f"Written {len(content)} bytes to {path}",
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="file_write",
            success=False,
            output=None,
            error=str(e),
        )


def file_grep(pattern: str, path: str, channel_id: str) -> ToolResult:
    """Search for a pattern in files within the context directory.

    Args:
        pattern: Regex pattern to search for
        path: Relative path within the context directory (file or directory)
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with matching lines or error
    """
    try:
        context_dir = _get_context_dir(channel_id)
        target_path = (context_dir / path).resolve()

        if not is_within_directory(target_path, context_dir):
            return ToolResult(
                name="file_grep",
                success=False,
                output=None,
                error="Only files within the context directory can be accessed",
            )

        if not target_path.exists():
            return ToolResult(
                name="file_grep",
                success=False,
                output=None,
                error=f"Path not found: {path}",
            )

        regex = re.compile(pattern)
        matches = []

        if target_path.is_file():
            files_to_search = [target_path]
        else:
            files_to_search = list(target_path.rglob("*"))

        for file_path in files_to_search:
            if not file_path.is_file():
                continue
            try:
                lines = file_path.read_text().splitlines()
                for i, line in enumerate(lines, 1):
                    if regex.search(line):
                        rel_path = file_path.relative_to(context_dir)
                        matches.append(f"{rel_path}:{i}: {line.rstrip()}")
            except (UnicodeDecodeError, PermissionError):
                continue

        output = "\n".join(matches) if matches else "No matches found"
        return ToolResult(
            name="file_grep",
            success=True,
            output=output,
            error=None,
        )

    except re.error as e:
        return ToolResult(
            name="file_grep",
            success=False,
            output=None,
            error=f"Invalid regex pattern: {e}",
        )
    except Exception as e:
        return ToolResult(
            name="file_grep",
            success=False,
            output=None,
            error=str(e),
        )


def file_list(path: str, channel_id: str) -> ToolResult:
    """List files and directories within the context directory.

    Args:
        path: Relative path within the context directory
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with directory listing or error
    """
    try:
        context_dir = _get_context_dir(channel_id)
        target_path = (context_dir / path).resolve()

        if not is_within_directory(target_path, context_dir):
            return ToolResult(
                name="file_list",
                success=False,
                output=None,
                error="Only files within the context directory can be accessed",
            )

        if not target_path.exists():
            return ToolResult(
                name="file_list",
                success=False,
                output=None,
                error=f"Path not found: {path}",
            )

        if not target_path.is_dir():
            return ToolResult(
                name="file_list",
                success=False,
                output=None,
                error=f"Not a directory: {path}",
            )

        entries = []
        for entry in sorted(target_path.iterdir()):
            rel_path = entry.relative_to(context_dir)
            if entry.is_dir():
                entries.append(f"{rel_path}/")
            else:
                entries.append(str(rel_path))

        output = "\n".join(entries) if entries else "(empty)"
        return ToolResult(
            name="file_list",
            success=True,
            output=output,
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="file_list",
            success=False,
            output=None,
            error=str(e),
        )


def file_search(pattern: str, path: str, channel_id: str) -> ToolResult:
    """Search for files matching a pattern within the context directory.

    Args:
        pattern: Glob pattern to match file/directory names
        path: Relative path within the context directory
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with matching paths or error
    """
    try:
        context_dir = _get_context_dir(channel_id)
        target_path = (context_dir / path).resolve()

        if not is_within_directory(target_path, context_dir):
            return ToolResult(
                name="file_search",
                success=False,
                output=None,
                error="Only files within the context directory can be accessed",
            )

        if not target_path.exists():
            return ToolResult(
                name="file_search",
                success=False,
                output=None,
                error=f"Path not found: {path}",
            )

        matches = []
        for match in target_path.rglob(pattern):
            if not is_within_directory(match, context_dir):
                continue
            rel_path = match.relative_to(context_dir)
            if match.is_dir():
                matches.append(f"{rel_path}/")
            else:
                matches.append(str(rel_path))

        matches.sort()
        output = "\n".join(matches) if matches else "No matches found"
        return ToolResult(
            name="file_search",
            success=True,
            output=output,
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="file_search",
            success=False,
            output=None,
            error=str(e),
        )


async def analyze_image(image_filename: str, image_query: str, channel_id: str) -> ToolResult:
    """Analyze an image using AI vision or retrieve a cached analysis.

    Args:
        image_filename: Filename of the image in attachments/ (e.g., 'abc123.jpg')
        image_query: What the user wants to know about the image
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with analysis or error
    """
    try:
        attachments_dir = _get_attachments_dir(channel_id)

        if ".." in image_filename or "/" in image_filename:
            return ToolResult(
                name="analyze_image",
                success=False,
                output=None,
                error="Invalid filename - path traversal not allowed",
            )

        target_path = (attachments_dir / image_filename).resolve()
        if not is_within_directory(target_path, attachments_dir):
            return ToolResult(
                name="analyze_image",
                success=False,
                output=None,
                error="Invalid filename - path traversal not allowed",
            )

        if not target_path.exists():
            return ToolResult(
                name="analyze_image",
                success=False,
                output=None,
                error=f"Image file not found: {image_filename}",
            )

        if not _is_image_file(image_filename):
            return ToolResult(
                name="analyze_image",
                success=False,
                output=None,
                error=f"File is not an image: {image_filename}",
            )

        from ..llm import LLMClient, VLLMClient

        cache_result = get_cached_image_analysis(image_filename, channel_id, image_query)
        if cache_result.success and cache_result.output and cache_result.output.startswith("CACHE_HIT:"):
            analysis_content = cache_result.output[len("CACHE_HIT:"):]
            lines = analysis_content.split("\n")
            result_lines = []
            skip = False
            for line in lines:
                if line.startswith("**User Query:**"):
                    skip = True
                elif line.startswith("**Timestamp:**"):
                    skip = True
                elif not skip and not line.startswith("### Analysis"):
                    result_lines.append(line)
            return ToolResult(
                name="analyze_image",
                success=True,
                output="[From cache]\n" + "\n".join(result_lines).strip(),
                error=None,
            )

        llm_client: LLMClient | VLLMClient
        if config.backend == "vllm":
            llm_client = VLLMClient(config.vllm)
            await llm_client.initialize()
            if config.vllm.vision_model:
                llm_client.model = config.vllm.vision_model
        else:
            llm_client = LLMClient(config.ollama)
        b64_image = llm_client.encode_image_base64(str(target_path))
        if not b64_image:
            return ToolResult(
                name="analyze_image",
                success=False,
                output=None,
                error=f"Failed to encode image: {image_filename}",
            )

        messages = [
            {"role": "user", "content": prompts.current().render("analyze-image", query=image_query), "images": [b64_image]}
        ]

        response_text, _ = await llm_client.chat(messages, [], channel_id)

        cache_write_result = append_image_analysis(image_filename, channel_id, image_query, response_text)
        if not cache_write_result.success:
            LOG.warning(f"Failed to cache analysis: {cache_write_result.error}")

        return ToolResult(
            name="analyze_image",
            success=True,
            output=response_text,
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="analyze_image",
            success=False,
            output=None,
            error=str(e),
        )