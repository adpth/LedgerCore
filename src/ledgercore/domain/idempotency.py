"""Canonical request fingerprinting for idempotency keys."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def canonical_json(value: object) -> str:
    """Serialise ``value`` as canonical JSON: sorted keys, no whitespace, no NaN/Infinity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def request_fingerprint(method: str, path: str, body: Mapping[str, object]) -> str:
    """Return a hex sha256 over the canonical JSON of (METHOD, path, body).

    The HTTP method is upper-cased; path and body are hashed verbatim. Key order and
    insignificant whitespace in the original payload therefore do not affect the result,
    while any change to a value does.
    """
    payload = {"method": method.upper(), "path": path, "body": dict(body)}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
