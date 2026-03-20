"""
processor.py — Pure formatting utilities.

Converts raw metric values into human-readable strings.
No I/O or side-effects.
"""

from typing import Union


def format_bytes(b: Union[int, float]) -> str:
    """Convert bytes to a human-readable string (e.g. '16.0 GB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def format_uptime(seconds: float) -> str:
    """Convert seconds to a human-readable duration (e.g. '2d 5h 12m')."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
