"""Moving between real values and placeholders (phase 7).

The PII service masks a document and returns {placeholder: real value}. The
models only ever see placeholders: `restore` puts the real values back into
what a model returned, for the code (ledger, policy, console), and `shield`
swaps them out again before anything goes to the next model."""
from typing import Any


def restore(obj: Any, mapping: dict[str, str]) -> Any:
    if not mapping:
        return obj
    if isinstance(obj, str):
        for token, value in mapping.items():
            obj = obj.replace(token, value)
        return obj
    if isinstance(obj, dict):
        return {k: restore(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [restore(v, mapping) for v in obj]
    return obj


def shield(obj: Any, mapping: dict[str, str]) -> Any:
    if not mapping:
        return obj
    if isinstance(obj, str):
        for token, value in sorted(mapping.items(), key=lambda kv: -len(kv[1])):  # longest first
            obj = obj.replace(value, token)
        return obj
    if isinstance(obj, dict):
        return {k: shield(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shield(v, mapping) for v in obj]
    return obj
