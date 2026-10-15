from __future__ import annotations

import json
import re

import pytest

from ledgercore.domain import request_fingerprint

BODY = {"amount_minor": 100, "currency": "USD", "meta": {"b": 1, "a": [1, 2]}}


def test_is_sha256_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", request_fingerprint("POST", "/transfers", BODY))


def test_deterministic() -> None:
    assert request_fingerprint("POST", "/t", BODY) == request_fingerprint("POST", "/t", BODY)


def test_key_order_does_not_matter_at_any_depth() -> None:
    reordered = {"meta": {"a": [1, 2], "b": 1}, "currency": "USD", "amount_minor": 100}
    assert request_fingerprint("POST", "/t", BODY) == request_fingerprint("POST", "/t", reordered)


def test_whitespace_in_original_json_does_not_matter() -> None:
    compact = json.loads('{"amount_minor":100,"currency":"USD","meta":{"b":1,"a":[1,2]}}')
    spaced = json.loads(
        '{\n  "meta" : { "a" : [ 1 , 2 ] , "b" : 1 } ,\n'
        '  "currency" : "USD",\t"amount_minor" : 100\n}'
    )
    assert request_fingerprint("POST", "/t", compact) == request_fingerprint("POST", "/t", spaced)


def test_method_case_is_normalised() -> None:
    assert request_fingerprint("post", "/t", BODY) == request_fingerprint("POST", "/t", BODY)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/t", BODY),
        ("POST", "/t2", BODY),
        ("POST", "/t", {**BODY, "amount_minor": 101}),
        ("POST", "/t", {**BODY, "amount_minor": "100"}),
        ("POST", "/t", {**BODY, "currency": "EUR"}),
        ("POST", "/t", {**BODY, "meta": {"b": 1, "a": [2, 1]}}),
        ("POST", "/t", {**BODY, "extra": None}),
        ("POST", "/t", {k: v for k, v in BODY.items() if k != "meta"}),
    ],
)
def test_any_value_change_changes_fingerprint(
    method: str, path: str, body: dict[str, object]
) -> None:
    assert request_fingerprint("POST", "/t", BODY) != request_fingerprint(method, path, body)


def test_rejects_nan() -> None:
    with pytest.raises(ValueError):
        request_fingerprint("POST", "/t", {"x": float("nan")})
