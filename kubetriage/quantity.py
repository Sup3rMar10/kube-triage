"""Kubernetes resource quantities ("500m", "128Mi", "1e3") to numbers, and back to readable text."""
from __future__ import annotations

import re

_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)$")

_SCALE = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
    "Ki": 2.0**10, "Mi": 2.0**20, "Gi": 2.0**30, "Ti": 2.0**40, "Pi": 2.0**50, "Ei": 2.0**60,
}


def parse_quantity(text) -> float:
    """Return the value in base units (cores for cpu, bytes for memory). Raises ValueError if malformed."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _RE.match(str(text).strip())
    if not m or m.group(2) not in _SCALE:
        raise ValueError(f"not a Kubernetes quantity: {text!r}")
    return float(m.group(1)) * _SCALE[m.group(2)]


def cpu_millicores(text) -> int:
    return int(round(parse_quantity(text) * 1000))


def mem_bytes(text) -> int:
    return int(round(parse_quantity(text)))


def fmt_cpu(millicores: int) -> str:
    if millicores % 1000 == 0:
        return f"{millicores // 1000} CPU" if millicores // 1000 == 1 else f"{millicores // 1000} CPUs"
    return f"{millicores}m"


def fmt_bytes(n: int) -> str:
    for unit, size in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if n >= size:
            value = n / size
            return f"{int(value)}{unit}" if value == int(value) else f"{value:.1f}{unit}"
    return f"{n}B"
