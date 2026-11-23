"""Observation boundary masking adds bare and partially masked PCI card formats."""
import re
from typing import Any

from ..filters.builtin.pii_mask import mask_text as core_mask_text, MASK_RULES


def mask_text(text: str) -> str:
    """Mask core PII plus unformatted full PANs and conventional last-four PAN displays."""
    text = core_mask_text(text)
    text = re.sub(r"\b\d{13,19}\b", "[REDACTED_CARD]", text)
    return re.sub(r"(?i)(?:\*{4}|x{4})[- ]+(?:\*{4}|x{4})[- ]+(?:\*{4}|x{4})[- ]+\d{4}",
                  "[REDACTED_CARD]", text)


def masker(value: Any) -> Any:
    """Preserve JSON structure while redacting string leaves before SDK serialization."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        sensitive = {"api_key", "password", "passwd", "secret", "authorization", "access_token", "refresh_token",
                     "token", "cvc", "cvv"}
        return {mask_text(key) if isinstance(key, str) else key:
                "[REDACTED_SECRET]" if isinstance(key, str) and key.lower().replace("-", "_") in sensitive
                else masker(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [masker(item) for item in value]
    return value


mask_any = masker

__all__ = ["masker", "mask_any", "mask_text", "MASK_RULES"]
