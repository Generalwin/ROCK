import re

_MEGABYTES_PER_GIGABYTE = 1024


def parse_size_to_bytes(size_str: str) -> int:
    size_str = size_str.strip().lower()
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "ki": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mi": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gi": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "ti": 1024**4,
    }

    match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)?$", size_str)
    if not match:
        raise ValueError(f"Invalid memory size format: {size_str}")
    number = float(match.group(1))
    unit = match.group(2) or "b"
    if unit not in units:
        raise ValueError(f"Unknown memory unit: {unit}")
    return int(number * units[unit])


def parse_size_to_mb(size_str: str | None) -> int:
    """Convert a size string (``8g``/``4096m``) to MB; a bare number is MB, empty returns 0."""
    if not size_str:
        return 0
    s = size_str.strip().lower()
    if re.match(r"^\d+(?:\.\d+)?$", s):
        return int(float(s))
    return parse_size_to_bytes(s) // (1024**2)


def megabytes_to_size(megabytes: int) -> str:
    if megabytes % _MEGABYTES_PER_GIGABYTE == 0:
        return f"{megabytes // _MEGABYTES_PER_GIGABYTE}g"
    return f"{megabytes}m"


def normalize_memory_to_k8s(memory: str) -> str:
    """Normalize a size string ('2g'/'2048m') to K8s format ('2Gi'/'2048Mi')."""
    if re.match(r"^\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki)$", memory):
        return memory
    match = re.match(r"^(\d+(\.\d+)?)([a-zA-Z]*)$", memory)
    if not match:
        try:
            return f"{int(memory) // (1024 * 1024)}Mi"
        except (ValueError, TypeError):
            return memory
    value = float(match.group(1))
    unit = match.group(3).lower()
    if unit in ("", "b"):
        mi = value / (1024 * 1024)
        return f"{int(mi) if mi == int(mi) else mi:.2f}Mi"
    if unit in ("k", "kb"):
        return f"{int(value) if value == int(value) else value:.2f}Ki"
    if unit in ("m", "mb"):
        return f"{int(value) if value == int(value) else value:.2f}Mi"
    if unit in ("g", "gb"):
        return f"{int(value) if value == int(value) else value:.2f}Gi"
    if unit in ("t", "tb"):
        return f"{int(value) if value == int(value) else value:.2f}Ti"
    return memory


def convert_to_gb(size_str: str) -> str:
    bytes_size = parse_size_to_bytes(size_str)
    gb_size = bytes_size / (1024**3)
    return f"{gb_size:.2f}g"
