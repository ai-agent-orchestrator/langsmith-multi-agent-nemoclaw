"""Single exception boundary for optional SDK operations."""
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def safe_call(function: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Return a fallback on SDK failure without logging arguments or exception text."""
    try:
        return function(*args, **kwargs)
    except Exception as error:
        logger.warning("observation unavailable (%s)", type(error).__name__)
        return default
