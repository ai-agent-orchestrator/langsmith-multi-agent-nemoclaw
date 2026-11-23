"""Online rules degrade to UI instructions when the installed SDK lacks support."""
from typing import Any

from .capabilities import sdk_call


def setup(client: Any, project: str, *, sample_rate: float = 0.1) -> Any:
    """Configure a sampled rule if supported, otherwise return the UI guide."""
    if not 0 <= sample_rate <= 1:
        raise ValueError("sample rate must be between zero and one")
    return sdk_call(client, "create_run_rule", project_name=project, sampling_rate=sample_rate,
                    filter='has(tags,"pattern:hierarchical")')
