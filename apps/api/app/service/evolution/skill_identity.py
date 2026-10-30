"""Credential-free identity for immutable Skill execution policy."""

import hashlib
import json
from typing import Any


def skill_identity(core: Any) -> str:
    """Fingerprint model endpoint/name and policy, never environment credentials."""
    model = core.model
    payload = {
        "contract_revision": 1,
        "model_kind": f"{type(model).__module__}.{type(model).__qualname__}",
        "model": getattr(model, "model", None),
        "endpoint": getattr(model, "base_url", None),
        "revision": getattr(model, "integration_revision", None),
        "strategy": core.strategy.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
