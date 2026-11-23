"""Bounded reads of registered catalog assets; no executable hooks."""
from pathlib import Path

MAX_ASSET_BYTES = 2_000_000


def read_asset(path: Path, root: Path) -> str:
    """Read UTF-8 only within the selected catalog root, including on subsequent loads."""
    try:
        resolved = path.resolve(strict=True)
        if root.absolute() not in resolved.parents or not resolved.is_file():
            raise ValueError("catalog asset outside root")
        with resolved.open("rb") as stream:
            data = stream.read(MAX_ASSET_BYTES + 1)
        if len(data) > MAX_ASSET_BYTES:
            raise ValueError("catalog asset exceeds size limit")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("catalog asset cannot be read") from None
