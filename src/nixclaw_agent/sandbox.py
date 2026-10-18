"""Run parsers of untrusted input (ffmpeg/ffprobe) without root.

Inbound voice messages are attacker-controlled files - in a 1:1 chat even
an UNKNOWN number gets them transcoded - and media demuxers are a classic
exploit target. The harness itself runs as root (PRIVILEGED users' bash
needs it), so a parser started from it is wrapped in:

- `setpriv`: uid/gid `nobody`, no supplementary groups, no capabilities,
  no_new_privs (no setuid escalation)
- `prlimit`: address space, CPU time and output file size caps

The input is passed as an already-open file descriptor (stdin, ffmpeg's
seekable `fd:` protocol), so the sandboxed process needs no read access to
channel data. Output goes into a private directory owned by `nobody`
(`convert_to_wav_chunks` chowns its output dir). The process can still read world-readable files and
use the network (no namespaces inside the default Docker seccomp profile).

Outside a root container (local runs, tests) the command runs unwrapped.
"""

import logging
import os
import shutil

LOG = logging.getLogger(__name__)

SANDBOX_UID = 65534  # nobody
SANDBOX_GID = 65534  # nogroup
MAX_ADDRESS_SPACE_BYTES = 2 * 1024**3
MAX_CPU_SECONDS = 120
MAX_FILE_BYTES = 512 * 1024**2

_warned = False


def enabled() -> bool:
    """True if commands can be wrapped (root and both tools installed)."""
    global _warned
    if os.geteuid() != 0:
        return False
    if shutil.which("setpriv") and shutil.which("prlimit"):
        return True
    if not _warned:
        LOG.warning("sandbox: setpriv/prlimit missing - running media parsers as root")
        _warned = True
    return False


def wrap(argv: list[str]) -> list[str]:
    """argv prefixed with the privilege drop + resource limits (if enabled)."""
    if not enabled():
        return argv
    return [
        "setpriv",
        f"--reuid={SANDBOX_UID}", f"--regid={SANDBOX_GID}", "--clear-groups",
        "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all",
        "prlimit",
        f"--as={MAX_ADDRESS_SPACE_BYTES}", f"--cpu={MAX_CPU_SECONDS}",
        f"--fsize={MAX_FILE_BYTES}",
        "--",
        *argv,
    ]


def env() -> dict[str, str]:
    """Minimal environment: the harness env may carry credentials."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LC_ALL": "C"}

