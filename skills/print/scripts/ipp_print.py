#!/usr/bin/env python3
"""
IPP Print Client - print via IPP Everywhere / AirPrint without a CUPS daemon.

Supports multi-page documents and real duplex by converting PDFs to
multi-page PWG Raster and submitting them as ONE IPP job.

Subcommands:
    probe     Query printer capabilities (ALWAYS run this first for a new printer)
    validate  Dry-run a job (Validate-Job) - checks attributes, uses no paper
    print     Convert if needed and submit a print job
    jobs      List jobs still in the printer queue
    cancel    Cancel one job or all jobs

Examples:
    ipp_print.py probe    ipp://printer.local:631/ipp/print
    ipp_print.py print    ipp://printer.local:631/ipp/print doc.pdf --duplex --color=monochrome
    ipp_print.py print    ipp://printer.local:631/ipp/print photo.jpg --quality=high --color=color
    ipp_print.py jobs     ipp://printer.local:631/ipp/print
    ipp_print.py cancel   ipp://printer.local:631/ipp/print all

Design notes (learned the hard way, see SKILL.md):
  * Every IPP attribute is written with an EXPLICIT value tag. Never rely on a
    library guessing the tag - a wrong/missing tag makes the attribute vanish
    silently and the job then prints with printer defaults.
  * The response's unsupported-attributes group is parsed and reported loudly.
    "successful-ok-ignored-or-substituted-attributes" means the printer threw
    some of your settings away - that is a FAILURE for our purposes.
  * Duplex requires ONE job containing ALL pages. One job per page can never
    produce a double-sided sheet.
  * Print-Job and Validate-Job carry ipp-attribute-fidelity=true, so the
    printer REJECTS a job it cannot print as requested instead of silently
    printing it with defaults.
  * Copies are ALWAYS made client-side (pages repeated inside the ONE raster
    job) and the `copies` attribute is never sent: the Brother MFC-J4540DW
    ignored copies=10 for image/pwg-raster and reported `copies` as unsupported,
    so with fidelity=true it would reject every job carrying it (incident
    2026-10-06: 9 copies became 21 jobs).

User consent (nixclaw harness contract, `print` only):
  * Without NIXCLAW_CONSENT=1 `print` validates and converts everything, writes
    a short English summary to stdout and the line
        NIXCLAW_REQUIRE_CONSENT_QUESTION_<crc>="<question>"
    to stderr, and exits 0 WITHOUT submitting a job. The harness then asks the
    users via a Signal poll (<crc> = $NIXCLAW_CHANNEL_ID, set by the bash tool).
  * With NIXCLAW_CONSENT=1 (set by the harness after "Ja") it writes the same
    question line again right before submitting ONE job, and
    NIXCLAW_CONSENT_OK_<crc> after a successful submit.

Requirements: python3, urllib (stdlib). For PDF input additionally
poppler-utils (pdfinfo), cups-filters (/usr/lib/cups/filter/pdftoraster)
and the `driverless` backend - all present in the nixclaw container.
"""

from __future__ import annotations

import argparse
import http.client
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# IPP constants
# --------------------------------------------------------------------------

TAG_OPERATION_ATTRS = 0x01
TAG_JOB_ATTRS = 0x02
TAG_END_OF_ATTRS = 0x03
TAG_PRINTER_ATTRS = 0x04
TAG_UNSUPPORTED_ATTRS = 0x05

TAG_UNSUPPORTED_VALUE = 0x10
TAG_NO_VALUE = 0x13
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_OCTET_STRING = 0x30
TAG_DATETIME = 0x31
TAG_RESOLUTION = 0x32
TAG_RANGE_OF_INTEGER = 0x33
TAG_BEG_COLLECTION = 0x34
TAG_END_COLLECTION = 0x37
TAG_MEMBER_NAME = 0x4A
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_NATURAL_LANGUAGE = 0x48
TAG_MIME_MEDIA_TYPE = 0x49

OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# RFC 8011 section 13.1 - the 0x04xx range is CLIENT errors, 0x05xx is SERVER
# errors. An earlier version of this script had these two swapped, which caused
# 0x040A (document-format-not-supported) to be misdiagnosed for weeks as a
# "clogged queue". Do not "fix" this table without checking the RFC.
IPP_STATUS = {
    0x0000: "successful-ok",
    0x0001: "successful-ok-ignored-or-substituted-attributes",
    0x0002: "successful-ok-conflicting-attributes",
    0x0400: "client-error-bad-request",
    0x0401: "client-error-forbidden",
    0x0402: "client-error-not-authenticated",
    0x0403: "client-error-not-authorized",
    0x0404: "client-error-not-possible",
    0x0405: "client-error-timeout",
    0x0406: "client-error-not-found",
    0x0407: "client-error-gone",
    0x0408: "client-error-request-entity-too-large",
    0x0409: "client-error-request-value-too-long",
    0x040A: "client-error-document-format-not-supported",
    0x040B: "client-error-attributes-or-values-not-supported",
    0x040C: "client-error-uri-scheme-not-supported",
    0x040D: "client-error-charset-not-supported",
    0x040E: "client-error-conflicting-attributes",
    0x040F: "client-error-compression-not-supported",
    0x0410: "client-error-compression-error",
    0x0411: "client-error-document-format-error",
    0x0412: "client-error-document-access-error",
    0x0500: "server-error-internal-error",
    0x0501: "server-error-operation-not-supported",
    0x0502: "server-error-service-unavailable",
    0x0503: "server-error-version-not-supported",
    0x0504: "server-error-device-error",
    0x0505: "server-error-temporary-error",
    0x0506: "server-error-not-accepting-jobs",
    0x0507: "server-error-busy",
    0x0508: "server-error-job-canceled",
    0x0509: "server-error-multiple-document-jobs-not-supported",
    0x050A: "server-error-printer-is-deactivated",
}

JOB_STATE = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}

PRINTER_STATE = {3: "idle", 4: "processing", 5: "stopped"}

# Explicit value tags. If an attribute is not listed here the script refuses to
# send it rather than guessing - silent drops are what broke duplex before.
ATTR_TAGS: Dict[str, int] = {
    "attributes-charset": TAG_CHARSET,
    "attributes-natural-language": TAG_NATURAL_LANGUAGE,
    "printer-uri": TAG_URI,
    "requesting-user-name": TAG_NAME,
    "job-name": TAG_NAME,
    "document-format": TAG_MIME_MEDIA_TYPE,
    "requested-attributes": TAG_KEYWORD,
    "which-jobs": TAG_KEYWORD,
    "job-id": TAG_INTEGER,
    "copies": TAG_INTEGER,
    "sides": TAG_KEYWORD,
    "media": TAG_KEYWORD,
    "print-color-mode": TAG_KEYWORD,
    "print-quality": TAG_ENUM,
    "printer-resolution": TAG_RESOLUTION,
    "orientation-requested": TAG_ENUM,
    "output-bin": TAG_KEYWORD,
    "media-source": TAG_KEYWORD,
    "identify-actions": TAG_KEYWORD,
    "ipp-attribute-fidelity": TAG_BOOLEAN,
}

QUALITY_ENUM = {"draft": 3, "normal": 4, "high": 5}  # RFC 8011: 3/4/5, NOT 1/2/3
SIDES_ALIASES = {
    "1": "one-sided",
    "simplex": "one-sided",
    "one-sided": "one-sided",
    "2": "two-sided-long-edge",
    "duplex": "two-sided-long-edge",
    "long": "two-sided-long-edge",
    "two-sided-long-edge": "two-sided-long-edge",
    "short": "two-sided-short-edge",
    "tumble": "two-sided-short-edge",
    "two-sided-short-edge": "two-sided-short-edge",
}
COLOR_ALIASES = {
    "color": "color",
    "colour": "color",
    "farbe": "color",
    "mono": "monochrome",       # legacy value - was never a valid IPP keyword
    "bw": "monochrome",
    "sw": "monochrome",
    "grey": "monochrome",
    "gray": "monochrome",
    "monochrome": "monochrome",
    "auto": "auto",
    "auto-monochrome": "auto-monochrome",
}

SIDES_LABEL = {
    "one-sided": "one-sided",
    "two-sided-long-edge": "two-sided",
    "two-sided-short-edge": "two-sided (short edge)",
}
COLOR_LABEL = {
    "monochrome": "b/w",
    "auto-monochrome": "b/w (auto)",
    "color": "color",
    "auto": "color (auto)",
}
QUALITY_LABEL = {"draft": "draft", "normal": "normal", "high": "high"}
DEFAULT_MEDIA = "iso_a4_210x297mm"

# --- nixclaw consent contract (see module docstring) ---
CHANNEL_ENV = "NIXCLAW_CHANNEL_ID"
CONSENT_ENV = "NIXCLAW_CONSENT"
CONSENT_QUESTION_PREFIX = "NIXCLAW_REQUIRE_CONSENT_QUESTION_"
CONSENT_OK_PREFIX = "NIXCLAW_CONSENT_OK_"
MAX_QUESTION_CHARS = 200
EXIT_NOT_VIA_AGENT = 2          # NIXCLAW_CHANNEL_ID missing/invalid
EXIT_SHARED_OUTPUT = 3          # stdout and stderr are the same file (2>&1, |&)

# Hard cap per `print` command (all copies included). The 2026-10-06 incident
# was a runaway loop; a cap turns any such mistake into an error, not paper.
MAX_SHEETS = 50

# Consent run: pause between the question marker and the Print-Job, so the
# harness can kill us on a mismatched or second marker before any job starts.
CONSENT_SUBMIT_DELAY_SECONDS = 1.5

PPD_CACHE_DIR = Path(os.environ.get("IPP_PPD_CACHE", "/app/tmp/ipp-ppd-cache"))
PDFTORASTER = "/usr/lib/cups/filter/pdftoraster"
DRIVERLESS_CANDIDATES = ["/usr/bin/driverless", "/usr/lib/cups/backend/driverless"]


# --------------------------------------------------------------------------
# IPP encoding
# --------------------------------------------------------------------------


def _encode_value(tag: int, value: Any) -> bytes:
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.pack(">H", 4) + struct.pack(">i", int(value))
    if tag == TAG_BOOLEAN:
        return struct.pack(">H", 1) + struct.pack(">b", 1 if value else 0)
    if tag == TAG_RESOLUTION:
        xres, yres = value if isinstance(value, (tuple, list)) else (value, value)
        return struct.pack(">H", 9) + struct.pack(">iib", int(xres), int(yres), 3)
    data = str(value).encode("utf-8")
    if len(data) > 0x7FFF:
        raise ValueError(f"IPP value too long ({len(data)} bytes)")
    return struct.pack(">H", len(data)) + data


def _encode_attr(name: str, value: Any) -> bytes:
    tag = ATTR_TAGS.get(name)
    if tag is None:
        raise ValueError(
            f"Refusing to send unknown IPP attribute '{name}' - add it to "
            f"ATTR_TAGS with the correct value tag first."
        )
    values = value if isinstance(value, (list, tuple)) else [value]
    encoded_name = name.encode("utf-8")
    out = b""
    for i, val in enumerate(values):
        out += struct.pack(">B", tag)
        if i == 0:
            out += struct.pack(">H", len(encoded_name)) + encoded_name
        else:
            out += struct.pack(">H", 0)
        out += _encode_value(tag, val)
    return out


def build_request(
    operation: int,
    operation_attrs: List[Tuple[str, Any]],
    job_attrs: Optional[List[Tuple[str, Any]]] = None,
    data: bytes = b"",
    request_id: int = 1,
) -> bytes:
    out = struct.pack(">BB", 1, 1)                 # IPP version 1.1
    out += struct.pack(">H", operation)
    out += struct.pack(">i", request_id)
    out += struct.pack(">B", TAG_OPERATION_ATTRS)
    for name, value in operation_attrs:
        out += _encode_attr(name, value)
    if job_attrs:
        out += struct.pack(">B", TAG_JOB_ATTRS)
        for name, value in job_attrs:
            out += _encode_attr(name, value)
    out += struct.pack(">B", TAG_END_OF_ATTRS)
    return out + data


# --------------------------------------------------------------------------
# IPP decoding
# --------------------------------------------------------------------------


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag in (TAG_INTEGER, TAG_ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == TAG_BOOLEAN and len(raw) == 1:
        return bool(raw[0])
    if tag == TAG_RESOLUTION and len(raw) == 9:
        x, y, unit = struct.unpack(">iib", raw)
        return f"{x}x{y}{'dpi' if unit == 3 else 'dpcm'}"
    if tag == TAG_RANGE_OF_INTEGER and len(raw) == 8:
        lo, hi = struct.unpack(">ii", raw)
        return f"{lo}-{hi}"
    if tag == TAG_NO_VALUE:
        return "no-value"
    if tag in (TAG_OCTET_STRING, TAG_DATETIME, TAG_BEG_COLLECTION, TAG_END_COLLECTION):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return raw.decode("utf-8", errors="replace")


class IppResponse:
    def __init__(self, status: int, groups: List[Tuple[int, Dict[str, Any]]]) -> None:
        self.status = status
        self.groups = groups

    @property
    def status_name(self) -> str:
        return IPP_STATUS.get(self.status, f"unknown-0x{self.status:04x}")

    @property
    def ok(self) -> bool:
        return self.status < 0x0100

    @property
    def substituted(self) -> bool:
        """Printer accepted the job but changed/ignored attributes we asked for."""
        return self.status in (0x0001, 0x0002)

    def merged(self, group_tag: Optional[int] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for tag, attrs in self.groups:
            if group_tag is None or tag == group_tag:
                out.update(attrs)
        return out

    def unsupported(self) -> Dict[str, Any]:
        return self.merged(TAG_UNSUPPORTED_ATTRS)

    def job_groups(self) -> List[Dict[str, Any]]:
        return [attrs for tag, attrs in self.groups if tag == TAG_JOB_ATTRS]


def parse_response(body: bytes) -> IppResponse:
    if len(body) < 8:
        raise ValueError(f"IPP response too short ({len(body)} bytes)")

    def need(offset: int, count: int) -> None:
        if offset + count > len(body):
            raise ValueError(
                f"truncated IPP response: need {count} bytes at offset {offset}, "
                f"only {len(body) - offset} left"
            )

    status = struct.unpack(">H", body[2:4])[0]
    groups: List[Tuple[int, Dict[str, Any]]] = []
    current_tag = 0
    current: Dict[str, Any] = {}
    last_name: Optional[str] = None
    collection_depth = 0
    offset = 8

    while offset < len(body):
        tag = body[offset]
        if tag == TAG_END_OF_ATTRS:
            break
        if tag < 0x10:  # delimiter -> start a new group
            if current:
                groups.append((current_tag, current))
            current_tag, current, last_name = tag, {}, None
            collection_depth = 0
            offset += 1
            continue

        offset += 1
        need(offset, 2)
        name_len = struct.unpack(">H", body[offset : offset + 2])[0]
        offset += 2
        need(offset, name_len)
        name = body[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len
        need(offset, 2)
        val_len = struct.unpack(">H", body[offset : offset + 2])[0]
        offset += 2
        need(offset, val_len)
        raw = body[offset : offset + val_len]
        offset += val_len

        # Collections (media-col-ready etc.): members are encoded with name-len 0
        # and would otherwise be folded into the previous attribute as garbage.
        if tag == TAG_BEG_COLLECTION:
            collection_depth += 1
            if collection_depth == 1 and name_len > 0:
                current[name] = "<collection>"
                last_name = None
            continue
        if tag == TAG_END_COLLECTION:
            collection_depth = max(0, collection_depth - 1)
            continue
        if collection_depth > 0 or tag == TAG_MEMBER_NAME:
            continue  # skip collection member names/values

        value = _decode_value(tag, raw)
        if name_len == 0:                            # additional value (1setOf)
            if last_name is None:
                continue                             # malformed; nothing to attach to
            prev = current[last_name]
            current[last_name] = prev + [value] if isinstance(prev, list) else [prev, value]
        else:
            current[name] = value
            last_name = name

    if current:
        groups.append((current_tag, current))
    return IppResponse(status, groups)


def http_url(printer_uri: str) -> str:
    """ipp://host[/path] -> http://host:631/path (IPv6-safe, keeps the resource)."""
    parts = urllib.parse.urlsplit(printer_uri)
    scheme = {"ipp": "http", "ipps": "https"}.get(parts.scheme, parts.scheme or "http")
    host = parts.hostname or parts.netloc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                            # IPv6 literal
    port = parts.port or (443 if scheme == "https" else 631)
    path = parts.path or "/ipp/print"
    return urllib.parse.urlunsplit((scheme, f"{host}:{port}", path, parts.query, ""))


def ipp_request(printer_uri: str, payload: bytes, timeout: int = 60) -> IppResponse:
    url = http_url(printer_uri)
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/ipp", "Content-Length": str(len(payload))},
        method="POST",
    )
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if url.startswith("https://"):
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False                    # printers ship self-signed certs
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["context"] = ctx
    try:
        with urllib.request.urlopen(req, **kwargs) as resp:
            return parse_response(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from printer: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach printer at {url}: {exc.reason}") from exc
    except (struct.error, OSError, http.client.HTTPException) as exc:
        raise RuntimeError(f"IPP transport error talking to {url}: {exc}") from exc
    except ValueError as exc:                         # garbled IPP reply
        raise RuntimeError(f"unreadable IPP response from {url}: {exc}") from exc


def base_op_attrs(printer_uri: str, user: str = "nixclaw") -> List[Tuple[str, Any]]:
    return [
        ("attributes-charset", "utf-8"),
        ("attributes-natural-language", "en"),
        ("printer-uri", printer_uri),
        ("requesting-user-name", user),
    ]


# --------------------------------------------------------------------------
# Printer capabilities
# --------------------------------------------------------------------------


def get_printer_attrs(printer_uri: str) -> Dict[str, Any]:
    payload = build_request(
        OP_GET_PRINTER_ATTRIBUTES,
        base_op_attrs(printer_uri) + [("requested-attributes", "all")],
    )
    return ipp_request(printer_uri, payload, timeout=10).merged(TAG_PRINTER_ATTRS)


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def marker_warnings(attrs: Dict[str, Any]) -> List[str]:
    names = as_list(attrs.get("marker-names"))
    levels = as_list(attrs.get("marker-levels"))
    lows = as_list(attrs.get("marker-low-levels"))
    out = []
    for i, name in enumerate(names):
        if i < len(levels):
            level = levels[i]
            low = lows[i] if i < len(lows) else 10
            if isinstance(level, int) and isinstance(low, int) and 0 <= level <= low:
                out.append(f"{name}={level}% (low threshold {low}%)")
    return out


# --------------------------------------------------------------------------
# Conversion: PDF/image -> multi-page PWG Raster
# --------------------------------------------------------------------------


def _run(cmd: List[str], env: Optional[Dict[str, str]] = None, stdout=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, env=env, stdout=stdout, stderr=subprocess.PIPE, check=False, timeout=600
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{os.path.basename(cmd[0])} timed out after 600s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"required helper not installed: {cmd[0]}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run {cmd[0]}: {exc}") from exc


def fetch_ppd(printer_uri: str) -> Path:
    """Generate a PPD straight from the printer's IPP attributes (no cupsd)."""
    PPD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = "".join(c if c.isalnum() else "_" for c in printer_uri)
    ppd = PPD_CACHE_DIR / f"{key}.ppd"
    if ppd.exists() and ppd.stat().st_size > 1000:
        return ppd
    driverless = next((p for p in DRIVERLESS_CANDIDATES if os.path.exists(p)), None)
    if not driverless:
        raise RuntimeError("`driverless` not found - cannot build a PPD for raster conversion")
    # Write to a private temp file and rename, so a crash or a concurrent run can
    # never leave a truncated PPD in the cache (that produces subtly wrong raster).
    tmp_ppd = ppd.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(tmp_ppd, "wb") as fh:
            proc = _run([driverless, printer_uri], stdout=fh)
        if proc.returncode != 0 or tmp_ppd.stat().st_size < 1000:
            raise RuntimeError(
                f"driverless failed: {proc.stderr.decode(errors='replace')[:300]}"
            )
        os.replace(tmp_ppd, ppd)
    finally:
        tmp_ppd.unlink(missing_ok=True)
    return ppd


def image_to_pdf(src: Path, workdir: Path) -> Path:
    """Scale an image onto a centred A4 page so the printer does not guess."""
    out = workdir / "image.pdf"
    convert = shutil.which("convert") or shutil.which("magick")
    if not convert:
        raise RuntimeError("ImageMagick not available to wrap image into a PDF")
    proc = _run([
        convert, "-units", "PixelsPerInch", "-density", "300", str(src),
        "-auto-orient", "-resize", "2480x3508>",
        "-background", "white", "-gravity", "center", "-extent", "2480x3508",
        str(out),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"image->pdf failed: {proc.stderr.decode(errors='replace')[:300]}")
    return out


def pdf_to_pwg(
    pdf: Path, printer_uri: str, sides: str, color: str, quality: str,
    media: str, workdir: Path,
) -> Path:
    ppd = fetch_ppd(printer_uri)
    duplex = {
        "one-sided": "None",
        "two-sided-long-edge": "DuplexNoTumble",
        "two-sided-short-edge": "DuplexTumble",
    }[sides]
    page_size = "A4" if media.startswith("iso_a4") else "Letter" if "letter" in media else "A4"
    options = " ".join([
        f"ColorModel={'Gray' if color in ('monochrome', 'auto-monochrome') else 'RGB'}",
        f"cupsPrintQuality={quality.capitalize()}",
        f"PageSize={page_size}",
        f"Duplex={duplex}",
    ])
    env = dict(os.environ)
    env.update({
        "PPD": str(ppd),
        "CONTENT_TYPE": "application/pdf",
        "FINAL_CONTENT_TYPE": "image/pwg-raster",
        "PRINTER": "ipp_print",
    })
    out = workdir / "job.pwg"
    if not os.path.exists(PDFTORASTER):
        raise RuntimeError(f"{PDFTORASTER} missing - install cups-filters")
    with open(out, "wb") as fh:
        proc = _run([PDFTORASTER, "1", "nixclaw", "ipp_print", "1", options, str(pdf)],
                    env=env, stdout=fh)
    if proc.returncode != 0 or out.stat().st_size == 0:
        raise RuntimeError(f"pdftoraster failed: {proc.stderr.decode(errors='replace')[-400:]}")
    with open(out, "rb") as fh:
        if fh.read(4) != b"RaS2":
            raise RuntimeError("pdftoraster did not produce PWG Raster (wrong FINAL_CONTENT_TYPE?)")
    return out


def pdf_page_count(pdf: Path) -> int:
    if not shutil.which("pdfinfo"):
        return 1
    proc = _run(["pdfinfo", str(pdf)], stdout=subprocess.PIPE)
    for line in proc.stdout.decode(errors="replace").splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return 1


# --------------------------------------------------------------------------
# PWG Raster page handling (client-side copies)
# --------------------------------------------------------------------------
# PWG 5102.4: "RaS2" sync word, then per page a 1796-byte big-endian header
# followed by PackBits-style compressed lines. There is no page index, so the
# page boundaries are found by walking the compressed line data.

PWG_SYNC = b"RaS2"
PWG_HEADER_SIZE = 1796
_PWG_OFF_HEIGHT = 376
_PWG_OFF_BITS_PER_PIXEL = 388
_PWG_OFF_BYTES_PER_LINE = 392
_PWG_OFF_COLOR_SPACE = 400
_PWG_OFF_TOTAL_PAGE_COUNT = 452
# Additive colour spaces (W, RGB, RGBA, sGray, sRGB, AdobeRGB): 0xFF is white.
# Everything else (K, CMYK, ...) is subtractive: 0x00 is white.
_PWG_ADDITIVE_SPACES = {0, 1, 2, 18, 19, 20}


def _u32(buf: bytes, offset: int) -> int:
    return int(struct.unpack_from(">I", buf, offset)[0])


def pwg_split_pages(data: bytes) -> List[Tuple[bytes, bytes]]:
    """Split a PWG raster stream into [(header, compressed_page_data), ...]."""
    if data[:4] != PWG_SYNC:
        raise ValueError("not a PWG raster stream (missing RaS2 sync word)")
    pages: List[Tuple[bytes, bytes]] = []
    size = len(data)
    offset = 4
    while offset < size:
        if offset + PWG_HEADER_SIZE > size:
            raise ValueError(f"truncated PWG page header (page {len(pages) + 1})")
        header = data[offset : offset + PWG_HEADER_SIZE]
        offset += PWG_HEADER_SIZE
        height = _u32(header, _PWG_OFF_HEIGHT)
        bytes_per_line = _u32(header, _PWG_OFF_BYTES_PER_LINE)
        unit = max(1, (_u32(header, _PWG_OFF_BITS_PER_PIXEL) + 7) // 8)
        if height == 0 or bytes_per_line == 0:
            raise ValueError(f"PWG page {len(pages) + 1} has an empty bitmap")
        start = offset
        line = 0
        while line < height:
            if offset >= size:
                raise ValueError(f"truncated PWG page data (page {len(pages) + 1})")
            line += data[offset] + 1                  # line repeat count
            offset += 1
            done = 0
            while done < bytes_per_line:
                if offset >= size:
                    raise ValueError(f"truncated PWG page data (page {len(pages) + 1})")
                code = data[offset]
                offset += 1
                if code == 128:                       # clear rest of line to white
                    done = bytes_per_line
                elif code < 128:                      # one pixel repeated code+1 times
                    offset += unit
                    done += (code + 1) * unit
                else:                                 # 257-code literal pixels
                    count = (257 - code) * unit
                    offset += count
                    done += count
            if offset > size:
                raise ValueError(f"truncated PWG page data (page {len(pages) + 1})")
        pages.append((header, data[start:offset]))
    if not pages:
        raise ValueError("PWG raster stream contains no pages")
    return pages


def pwg_blank_page(header: bytes) -> bytes:
    """Compressed data for an all-white page with the given header's geometry."""
    height = _u32(header, _PWG_OFF_HEIGHT)
    bytes_per_line = _u32(header, _PWG_OFF_BYTES_PER_LINE)
    unit = max(1, (_u32(header, _PWG_OFF_BITS_PER_PIXEL) + 7) // 8)
    white = b"\xff" if _u32(header, _PWG_OFF_COLOR_SPACE) in _PWG_ADDITIVE_SPACES else b"\x00"
    line = bytearray()
    remaining = bytes_per_line // unit
    while remaining > 0:
        run = min(128, remaining)
        line.append(run - 1)
        line += white * unit
        remaining -= run
    out = bytearray()
    done = 0
    while done < height:
        repeat = min(256, height - done)
        out.append(repeat - 1)
        out += line
        done += repeat
    return bytes(out)


def pwg_repeat_copies(
    pages: List[Tuple[bytes, bytes]], copies: int, pad_to_even: bool,
) -> bytes:
    """Join split pages (from pwg_split_pages) `copies` times into one PWG stream.

    pad_to_even (duplex): an odd page count gets a blank back page per copy, so
    every copy starts on a fresh sheet and front/back parity is preserved.
    """
    if pad_to_even and len(pages) % 2:
        last_header = pages[-1][0]
        pages = pages + [(last_header, pwg_blank_page(last_header))]
    total = len(pages) * copies
    fixed: List[bytes] = []
    for header, body in pages:
        if _u32(header, _PWG_OFF_TOTAL_PAGE_COUNT):   # 0 = unknown, leave it alone
            header = (header[:_PWG_OFF_TOTAL_PAGE_COUNT] + struct.pack(">I", total)
                      + header[_PWG_OFF_TOTAL_PAGE_COUNT + 4:])
        fixed.append(header + body)
    return PWG_SYNC + b"".join(fixed) * copies


def prepare_document(
    src: Path, printer_uri: str, fmt: str, sides: str, color: str,
    quality: str, media: str, formats: List[str], workdir: Path,
    copies: int = 1,
) -> Tuple[Path, str]:
    """Return (file_to_send, document-format).

    copies > 1 forces the raster path: copies are made client-side by repeating
    the raster pages, which a single JPEG cannot do.
    """
    ext = src.suffix.lower()
    is_pdf = ext == ".pdf"
    duplex_wanted = sides != "one-sided"
    pages = pdf_page_count(src) if is_pdf else 1

    if fmt == "auto":
        needs_raster = is_pdf or duplex_wanted or pages > 1 or copies > 1
        if "image/pwg-raster" in formats and needs_raster:
            fmt = "raster"
        elif needs_raster:
            raise RuntimeError(
                "Printer does not accept image/pwg-raster, so multi-page/duplex "
                "printing (and client-side copies) is not possible via this path."
            )
        else:
            fmt = "jpeg"

    if fmt == "jpeg" and copies > 1:
        raise RuntimeError(
            "--format=jpeg cannot make several copies (copies are made client-side "
            "in a raster job); use --format=auto or --format=raster"
        )

    if fmt == "raster":
        pdf = src if is_pdf else image_to_pdf(src, workdir)
        return pdf_to_pwg(pdf, printer_uri, sides, color, quality, media, workdir), "image/pwg-raster"

    if fmt == "jpeg":
        if duplex_wanted:
            raise RuntimeError("JPEG is single-page only - duplex needs --format=raster")
        if ext in (".jpg", ".jpeg"):
            return src, "image/jpeg"
        out = workdir / "page.jpg"
        convert = shutil.which("convert") or shutil.which("magick")
        if not convert:
            raise RuntimeError("ImageMagick not available for JPEG conversion")
        proc = _run([
            convert, "-density", "300", str(src), "-auto-orient", "-quality", "92",
            "-resize", "2480x3508>", "-background", "white", "-gravity", "center",
            "-extent", "2480x3508", "-flatten", str(out),
        ])
        if proc.returncode != 0:
            raise RuntimeError(f"JPEG conversion failed: {proc.stderr.decode(errors='replace')[:300]}")
        return out, "image/jpeg"

    raise RuntimeError(f"Unknown --format={fmt}")


# --------------------------------------------------------------------------
# Job attribute assembly + reporting
# --------------------------------------------------------------------------


def job_attributes(
    args: argparse.Namespace, caps: Dict[str, Any], notes: List[str],
) -> List[Tuple[str, Any]]:
    """Job attributes with explicit tags; `notes` collects user-facing hints.

    `copies` is deliberately NOT sent (IPP default 1): copies are made
    client-side, and with ipp-attribute-fidelity=true a printer that lists
    `copies` as unsupported (the Brother) would reject the whole job.
    """
    attrs: List[Tuple[str, Any]] = []
    supported_sides = as_list(caps.get("sides-supported"))
    supported_color = as_list(caps.get("print-color-mode-supported"))
    supported_media = as_list(caps.get("media-supported"))

    if supported_sides and args.sides not in supported_sides:
        raise RuntimeError(
            f"Printer does not support sides={args.sides}. Supported: {supported_sides}"
        )
    attrs.append(("sides", args.sides))

    if supported_color and args.color not in supported_color:
        fallback = "monochrome" if "monochrome" in supported_color else supported_color[0]
        msg = (f"Note: {COLOR_LABEL.get(args.color, args.color)} is not supported by the printer, "
               f"using {COLOR_LABEL.get(fallback, fallback)} instead.")
        notes.append(msg)
        args.color = fallback
    attrs.append(("print-color-mode", args.color))

    if supported_media and args.media not in supported_media:
        # ipp-attribute-fidelity=true makes the printer reject it anyway - fail
        # now (before anyone is asked to approve) instead of after approval.
        raise RuntimeError(
            f"Printer does not support media={args.media}. Supported: {supported_media}"
        )
    attrs.append(("media", args.media))

    quality_enum = QUALITY_ENUM[args.quality]
    supported_quality = as_list(caps.get("print-quality-supported"))
    if supported_quality and quality_enum not in supported_quality:
        raise RuntimeError(
            f"Printer does not support --quality={args.quality} (enum {quality_enum}). "
            f"Supported enums: {supported_quality} (3=draft, 4=normal, 5=high)"
        )
    attrs.append(("print-quality", quality_enum))
    return attrs


def report_unsupported(resp: IppResponse) -> bool:
    """Return True if the printer dropped/substituted any attribute we asked for."""
    unsupported = resp.unsupported()
    unsupported.pop("attributes-charset", None)
    unsupported.pop("attributes-natural-language", None)
    if unsupported:
        print("  !! Printer REJECTED these attributes:")
        for key, value in unsupported.items():
            print(f"       {key} = {value}")
        return True
    if resp.substituted:
        # Status says "I changed something" even though no attribute was named.
        print(f"  !! Printer reports {resp.status_name} - it silently changed some setting.")
        return True
    return False


def poll_job(printer_uri: str, job_id: int, timeout: int = 300) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        payload = build_request(
            OP_GET_JOB_ATTRIBUTES, base_op_attrs(printer_uri) + [("job-id", job_id)]
        )
        resp = ipp_request(printer_uri, payload)
        last = resp.merged(TAG_JOB_ATTRS)
        state = last.get("job-state")
        name = _state_name(state)
        done = last.get("job-impressions-completed", "?")
        print(f"  job {job_id}: {name} (impressions completed: {done})")
        if state in (7, 8, 9):
            return last
        time.sleep(5)
    print(f"  job {job_id}: still not finished after {timeout}s")
    return last


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    caps = get_printer_attrs(args.printer_uri)
    interesting = [
        "printer-make-and-model", "printer-name", "printer-state",
        "printer-state-reasons", "printer-is-accepting-jobs", "queued-job-count",
        "document-format-supported", "document-format-preferred",
        "sides-supported", "print-color-mode-supported", "print-quality-supported",
        "printer-resolution-supported", "media-default", "media-ready",
        "operations-supported", "pdf-versions-supported", "urf-supported",
        "job-creation-attributes-supported",
    ]
    print(f"=== {args.printer_uri} ===")
    for key in interesting:
        if key in caps:
            value = caps[key]
            if key == "printer-state":
                value = f"{value} ({PRINTER_STATE.get(value, '?')})"
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            print(f"{key:38} {value}")

    formats = as_list(caps.get("document-format-supported"))
    print("\n--- what this means for us ---")
    print(f"multi-page/duplex via PWG Raster : {'YES' if 'image/pwg-raster' in formats else 'no'}")
    print(f"native PDF printing              : {'YES' if 'application/pdf' in formats else 'NO - must rasterise'}")
    print(f"duplex supported                 : {'YES' if any('two-sided' in str(s) for s in as_list(caps.get('sides-supported'))) else 'no'}")
    warn = marker_warnings(caps)
    if warn:
        print(f"LOW INK/TONER                    : {', '.join(warn)}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    payload = build_request(
        OP_GET_JOBS,
        base_op_attrs(args.printer_uri)
        + [("requested-attributes", ["job-id", "job-state", "job-state-reasons", "job-name"])],
    )
    resp = ipp_request(args.printer_uri, payload)
    groups = resp.job_groups()
    if not groups:
        print("Queue is empty.")
        return 0
    for job in groups:
        state = job.get("job-state")
        print(f"  job {job.get('job-id')}: {_state_name(state)} "
              f"name={job.get('job-name')} reasons={job.get('job-state-reasons')}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    if args.job_id == "all":
        payload = build_request(
            OP_GET_JOBS,
            base_op_attrs(args.printer_uri) + [("requested-attributes", ["job-id", "job-state"])],
        )
        raw_ids = [j.get("job-id") for j in ipp_request(args.printer_uri, payload).job_groups()]
        ids = [i for i in raw_ids if isinstance(i, int)]
        if len(ids) != len(raw_ids):
            print(f"WARNING: {len(raw_ids) - len(ids)} job(s) reported no job-id; skipping those")
    else:
        try:
            ids = [int(args.job_id)]
        except ValueError:
            raise RuntimeError(f"invalid job id '{args.job_id}' (use a number or 'all')") from None
    if not ids:
        print("Nothing to cancel.")
        return 0
    for job_id in ids:
        payload = build_request(
            OP_CANCEL_JOB, base_op_attrs(args.printer_uri) + [("job-id", int(job_id))]
        )
        resp = ipp_request(args.printer_uri, payload)
        print(f"  cancel job {job_id}: {resp.status_name}")
    return 0


@dataclass
class PreparedJob:
    """Everything needed to submit (or validate) one job, computed up front so
    that the consent run and the approved run agree on what gets printed."""

    src: Path
    data: bytes
    doc_format: str
    op_attrs: List[Tuple[str, Any]]
    job_attrs: List[Tuple[str, Any]]
    pages: int
    copies: int
    sheets: int
    notes: List[str]


def sheet_count(pages: int, copies: int, sides: str) -> int:
    per_copy = pages if sides == "one-sided" else (pages + 1) // 2
    return per_copy * copies


def printer_notes(caps: Dict[str, Any]) -> List[str]:
    """Variable, user-facing status hints (never part of the consent question)."""
    notes: List[str] = []
    if caps.get("printer-state") == 5:
        notes.append(f"Warning: the printer reports a fault "
                     f"({caps.get('printer-state-reasons')}).")
    queued = caps.get("queued-job-count", 0)
    if isinstance(queued, int) and queued > 0:
        notes.append(f"Note: {queued} job(s) already waiting in the printer.")
    warn = marker_warnings(caps)
    if warn:
        low = ", ".join(w.split(" (")[0].replace("=", " ") for w in warn)
        notes.append(f"Note: ink nearly empty: {low}")
    return notes


def prepare_job(args: argparse.Namespace, caps: Dict[str, Any], workdir: Path) -> PreparedJob:
    src = Path(args.file)
    if not src.is_file():
        raise RuntimeError(f"file not found: {src}")
    formats = [str(f) for f in as_list(caps.get("document-format-supported"))]
    notes = printer_notes(caps)
    # Validate sides/colour/media/quality first: a colour fallback must be
    # applied BEFORE rasterising.
    job_attrs = job_attributes(args, caps, notes)

    doc, doc_format = prepare_document(
        src, args.printer_uri, args.format, args.sides, args.color,
        args.quality, args.media, formats, workdir,
        copies=args.copies,
    )
    if doc_format not in formats:
        raise RuntimeError(f"printer does not accept {doc_format}. Supported: {formats}")
    data = doc.read_bytes()
    raster_pages = pwg_split_pages(data) if doc_format == "image/pwg-raster" else None
    pages = len(raster_pages) if raster_pages is not None else 1

    sheets = sheet_count(pages, args.copies, args.sides)
    if sheets > MAX_SHEETS:
        raise RuntimeError(
            f"{pages} page(s) x {args.copies} copies = {sheets} sheets exceeds the hard "
            f"limit of {MAX_SHEETS} sheets per print command. Nothing was printed. "
            f"Ask the user to reduce copies/pages or to split the document."
        )

    if args.copies > 1:                               # always client-side
        if raster_pages is None:
            raise RuntimeError(f"cannot make {args.copies} copies with {doc_format}")
        data = pwg_repeat_copies(raster_pages, args.copies,
                                 pad_to_even=args.sides != "one-sided")
    op_attrs = base_op_attrs(args.printer_uri) + [
        ("job-name", args.job_name or src.name[:60]),
        ("document-format", doc_format),
        # Reject instead of printing with defaults (see module docstring).
        ("ipp-attribute-fidelity", True),
    ]
    return PreparedJob(
        src=src, data=data, doc_format=doc_format, op_attrs=op_attrs,
        job_attrs=job_attrs, pages=pages, copies=args.copies,
        sheets=sheets, notes=notes,
    )


def _clean_text(text: str) -> str:
    """Single line, printable, no double quotes (consent question contract)."""
    out = "".join(ch if ch.isprintable() else " " for ch in text).replace('"', "'")
    return " ".join(out.split())


def _sheets_de(sheets: int) -> str:
    return "1 sheet" if sheets == 1 else f"{sheets} sheets"


def _settings_de(args: argparse.Namespace) -> List[str]:
    parts = [
        SIDES_LABEL.get(args.sides, args.sides),
        COLOR_LABEL.get(args.color, args.color),
        QUALITY_LABEL.get(args.quality, args.quality),
    ]
    if args.media != DEFAULT_MEDIA:
        parts.append(_clean_text(args.media))
    return parts


def consent_question(job: PreparedJob, args: argparse.Namespace) -> str:
    """Deterministic English yes/no question: same file + options -> same text.

    Never put printer state (ink, queue) or anything time-dependent in here:
    the harness compares the approved question with the consent run's.
    """
    parts = ([f"{job.copies} copies"] if job.copies > 1 else []) \
        + [_sheets_de(job.sheets)] + _settings_de(args)
    tail = " print: " + ", ".join(parts) + "?"
    name = _clean_text(job.src.name) or "Dokument"
    room = MAX_QUESTION_CHARS - len(tail)
    if len(name) > room:
        name = name[: max(1, room - 1)] + "…"
    return (name + tail)[:MAX_QUESTION_CHARS]


def info_lines(job: PreparedJob, args: argparse.Namespace) -> List[str]:
    """Short English summary for the chat (stdout)."""
    pages = "1 page" if job.pages == 1 else f"{job.pages} pages"
    amount = f"{pages} × {job.copies} copies" if job.copies > 1 else pages
    settings = ", ".join(_settings_de(args))
    lines = [
        f"Print job: {_clean_text(job.src.name) or 'Dokument'}",
        f"{amount} = {_sheets_de(job.sheets)} ({settings})",
    ]
    return lines + job.notes


def emit_marker(line: str) -> None:
    sys.stdout.flush()               # info first, then the marker (harness kills on it)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def stdout_is_stderr() -> bool:
    """True if fd 1 and fd 2 are the same file (2>&1, |&, ...). The harness needs
    to read the consent marker from stderr separately from the output."""
    try:
        out, err = os.fstat(1), os.fstat(2)
    except OSError:
        return True
    return (out.st_dev, out.st_ino) == (err.st_dev, err.st_ino)


def _make_workdir(args: argparse.Namespace) -> Path:
    try:
        Path(args.tmpdir).mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="ipp_print_", dir=args.tmpdir))
    except OSError as exc:
        raise RuntimeError(f"cannot create work dir in {args.tmpdir}: {exc}") from exc


def _cleanup(args: argparse.Namespace, workdir: Path) -> None:
    if args.keep_temp:
        print(f"  (temp files kept in {workdir})")
    else:
        shutil.rmtree(workdir, ignore_errors=True)


def _state_name(state: Any) -> str:
    return JOB_STATE.get(state, str(state)) if isinstance(state, int) else str(state)


def submit_job(args: argparse.Namespace, job: PreparedJob) -> int:
    """Send ONE Print-Job. 0 only if the printer accepted it as requested."""
    payload = build_request(OP_PRINT_JOB, job.op_attrs, job.job_attrs, data=job.data)
    try:
        resp = ipp_request(args.printer_uri, payload, timeout=args.timeout)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        print("It is UNKNOWN whether the printer received the job. Run `jobs` before "
              "anything else and do NOT submit again blindly.")
        return 1
    print(f"  status: {resp.status_name} (0x{resp.status:04x})")
    dropped = report_unsupported(resp)
    if not resp.ok:
        if resp.status == 0x040A:
            print("  -> The printer does not accept this document format.")
        elif resp.status in (0x040B, 0x040E):
            print("  -> The printer refused the job because it cannot print it with these "
                  "settings (ipp-attribute-fidelity). Nothing was printed. Pick values "
                  "from `probe`.")
        print("Error: the job was NOT accepted.")
        return 1

    job_info = resp.merged(TAG_JOB_ATTRS)
    job_id = job_info.get("job-id")
    if dropped:
        # The printer ignored ipp-attribute-fidelity and accepted the job with
        # other settings. Cancel it rather than print something nobody approved.
        print(f"Error: the printer accepted job {job_id} but changed the settings; "
              "cancelling it.")
        if isinstance(job_id, int):
            try:
                cancel = ipp_request(args.printer_uri, build_request(
                    OP_CANCEL_JOB, base_op_attrs(args.printer_uri) + [("job-id", job_id)]))
                print(f"  cancel job {job_id}: {cancel.status_name}")
            except RuntimeError as exc:
                print(f"  cancel job {job_id} FAILED: {exc} - run `jobs` / `cancel`.")
        else:
            print("  No job-id came back - run `jobs` to find and cancel it.")
        print("Some pages may already have been printed. Tell the users; do NOT print "
              "again without asking them.")
        return 1

    print(f"Sent: job {job_id} accepted by the printer.")
    if not isinstance(job_id, int):
        return 0
    try:
        if args.wait:
            final = poll_job(args.printer_uri, job_id, timeout=args.timeout)
        else:                        # one quick check; `jobs` shows progress later
            final = ipp_request(args.printer_uri, build_request(
                OP_GET_JOB_ATTRIBUTES, base_op_attrs(args.printer_uri) + [("job-id", job_id)]
            ), timeout=10).merged(TAG_JOB_ATTRS)
            state_now = final.get("job-state")
            print(f"  job {job_id}: {_state_name(state_now)}")
    except RuntimeError as exc:
        print(f"  (status check failed: {exc}; the job WAS submitted - use `jobs`)")
        return 0
    state = final.get("job-state")
    if state in (7, 8) or (args.wait and state not in (5, 9, None)):
        print(f"Error: job {job_id} ended as {_state_name(state)} "
              f"reasons={final.get('job-state-reasons')}")
        return 1
    return 0


def cmd_print(args: argparse.Namespace) -> int:
    crc = os.environ.get(CHANNEL_ENV, "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}", crc):
        print("Error: `print` must be run via the agent's bash tool (NIXCLAW_CHANNEL_ID "
              "missing or invalid): the harness asks the users for approval before "
              "anything is printed. Nothing was printed. For a dry run use `validate`.")
        return EXIT_NOT_VIA_AGENT
    if stdout_is_stderr():
        print("Error: run the `print` command standalone - no `2>&1`, pipes (`|`, `|&`), "
              "redirects, `nohup` or `&`, and only one command per bash call. The harness "
              "reads stdout and stderr separately to ask the users for approval. "
              "Nothing was printed.")
        return EXIT_SHARED_OUTPUT
    consent = os.environ.get(CONSENT_ENV) == "1"

    caps = get_printer_attrs(args.printer_uri)
    workdir = _make_workdir(args)
    cleaned = False
    try:
        job = prepare_job(args, caps, workdir)
        question = consent_question(job, args)
        print("\n".join(info_lines(job, args)))
        marker = f'{CONSENT_QUESTION_PREFIX}{crc}="{question}"'
        if not consent:
            # The harness kills the process group as soon as it reads the
            # marker, so clean up the temp dir first.
            _cleanup(args, workdir)
            cleaned = True
            emit_marker(marker)
            return 0
        # Printed on BOTH runs: on the consent run it tells the harness exactly
        # which approved action is about to happen (one approval = one job).
        emit_marker(marker)
        # Give the harness time to kill us on a mismatched/second marker
        # BEFORE any Print-Job starts.
        time.sleep(CONSENT_SUBMIT_DELAY_SECONDS)
        rc = submit_job(args, job)
        if rc == 0:
            emit_marker(f"{CONSENT_OK_PREFIX}{crc}")
        return rc
    finally:
        if not cleaned:
            _cleanup(args, workdir)


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate-Job dry run: no consent needed, no paper, no data sent."""
    caps = get_printer_attrs(args.printer_uri)
    workdir = _make_workdir(args)
    try:
        job = prepare_job(args, caps, workdir)
        print("\n".join(info_lines(job, args)))
        print(f"Validating: {job.src.name} -> {job.doc_format} "
              f"({len(job.data) // 1024} KiB would be sent, copies client-side)")
        payload = build_request(OP_VALIDATE_JOB, job.op_attrs, job.job_attrs)
        resp = ipp_request(args.printer_uri, payload, timeout=args.timeout)
        print(f"  status: {resp.status_name} (0x{resp.status:04x})")
        dropped = report_unsupported(resp)
        if not resp.ok or dropped:
            print("  Validate-Job FAILED (see above).")
            return 1
        print("  Validate-Job OK - no paper used.")
        return 0
    finally:
        _cleanup(args, workdir)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_job_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--sides", default="one-sided")
    parser.add_argument("--duplex", action="store_true",
                        help="shorthand for --sides=two-sided-long-edge")
    parser.add_argument("--color", default="monochrome",
                        help="color|monochrome|auto|auto-monochrome (default: monochrome)")
    parser.add_argument("--quality", default="normal", choices=list(QUALITY_ENUM))
    parser.add_argument("--media", default="iso_a4_210x297mm")
    parser.add_argument("--format", default="auto", choices=["auto", "raster", "jpeg"])
    parser.add_argument("--job-name", default=None)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--tmpdir", default="/app/tmp")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--wait", dest="wait", action="store_true", default=False,
                        help="poll the job until it finishes (slow; default: one status check)")
    parser.add_argument("--no-wait", dest="wait", action="store_false",
                        help="no-op alias kept for compatibility (not waiting is the default)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print via IPP without CUPS (multi-page + duplex capable)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="show printer capabilities")
    p_probe.add_argument("printer_uri")
    p_probe.set_defaults(func=cmd_probe)

    p_jobs = sub.add_parser("jobs", help="list queued jobs")
    p_jobs.add_argument("printer_uri")
    p_jobs.set_defaults(func=cmd_jobs)

    p_cancel = sub.add_parser("cancel", help="cancel a job id or 'all'")
    p_cancel.add_argument("printer_uri")
    p_cancel.add_argument("job_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_print = sub.add_parser("print", help="convert and print a file")
    p_print.add_argument("printer_uri")
    add_job_options(p_print)
    p_print.set_defaults(func=cmd_print)

    p_val = sub.add_parser("validate", help="dry-run a job (no paper)")
    p_val.add_argument("printer_uri")
    add_job_options(p_val)
    p_val.set_defaults(func=cmd_validate)

    for stream in (sys.stdout, sys.stderr):
        try:                       # non-ASCII text + consent marker are UTF-8
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    args = parser.parse_args()

    if getattr(args, "file", None) is not None:
        # Normalise aliases FIRST, then apply --duplex. Doing it the other way round
        # meant `--sides=simplex --duplex` silently stayed one-sided.
        raw_sides = str(args.sides).lower()
        args.sides = SIDES_ALIASES.get(raw_sides, args.sides)
        args.color = COLOR_ALIASES.get(str(args.color).lower(), args.color)
        if args.sides not in set(SIDES_ALIASES.values()):
            print(f"Error: invalid --sides={args.sides}")
            return 1
        if args.color not in set(COLOR_ALIASES.values()):
            print(f"Error: invalid --color={args.color}")
            return 1
        if args.duplex:
            if args.sides == "one-sided":
                args.sides = "two-sided-long-edge"
            elif args.sides != "two-sided-long-edge":
                print(f"Error: --duplex conflicts with --sides={args.sides}; pick one")
                return 1
        if args.copies < 1:
            print(f"Error: --copies must be >= 1, got {args.copies}")
            return 1
        if args.copies > MAX_SHEETS:
            print(f"Error: --copies={args.copies} exceeds the hard limit of "
                  f"{MAX_SHEETS} sheets per print command")
            return 1

    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, struct.error, OSError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
