"""Shared path-containment check used by every per-channel sandbox guard.

`str.startswith` is a plain string-prefix test and can be bypassed by a
sibling directory (e.g. base=/data, candidate=/data_escaped both "start
with" /data). This resolves both sides and checks true containment.
"""

from pathlib import Path


def is_within_directory(path: Path, directory: Path) -> bool:
    """True if `path` resolves to a location inside (or equal to) `directory`."""
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False
