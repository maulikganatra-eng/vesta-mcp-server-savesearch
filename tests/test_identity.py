"""Unit tests for the ``_meta`` bearer-token reader (step N1 / VA-396).

These are level 1: no protocol, no network. The protocol-level proof that ``_meta``
actually carries the token lives in ``tests/component/test_meta_channel.py``, and it
is the one that matters — everything here is about the reader's own edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from vesta_saved_search.identity import META_TOKEN_KEY, bearer_token_from_meta

pytestmark = pytest.mark.unit

TOKEN = "eyJhbGciOi.fake-but-realistically-shaped.signature"


class _FakeMeta:
    """Stands in for ``RequestParams.Meta``, whose extras land in ``model_extra``."""

    def __init__(self, extra: dict[str, Any] | None) -> None:
        self.model_extra = extra


class _FakeRequestContext:
    def __init__(self, meta: object) -> None:
        self.meta = meta


class _FakeCtx:
    """A context whose ``request_context`` resolves normally."""

    def __init__(self, meta: object) -> None:
        self.request_context = _FakeRequestContext(meta)


class _NoRequestCtx:
    """A context with no active request — accessing ``request_context`` raises.

    This is what the SDK really does outside a request, and it is why the reader
    catches rather than checks for None.
    """

    @property
    def request_context(self) -> Any:
        raise LookupError("no active request context")


def test_token_present_is_returned() -> None:
    ctx = _FakeCtx(_FakeMeta({META_TOKEN_KEY: TOKEN}))
    assert bearer_token_from_meta(ctx) == TOKEN


def test_token_returned_verbatim_not_normalised() -> None:
    """Surrounding whitespace is preserved, because the upstream must see it as sent.

    A server that silently trims a credential can turn a valid token into a 401 that
    nobody can reproduce. Blank-only values are a different case — see below.
    """
    padded = f"  {TOKEN}  "
    ctx = _FakeCtx(_FakeMeta({META_TOKEN_KEY: padded}))
    assert bearer_token_from_meta(ctx) == padded


@pytest.mark.parametrize(
    ("description", "ctx"),
    [
        ("meta absent entirely", _FakeCtx(None)),
        ("meta present, no extras at all", _FakeCtx(_FakeMeta(None))),
        ("meta present, extras empty", _FakeCtx(_FakeMeta({}))),
        ("extras present, our key absent", _FakeCtx(_FakeMeta({"progressToken": "abc"}))),
        ("no active request context", _NoRequestCtx()),
    ],
)
def test_absent_token_is_none_and_never_raises(description: str, ctx: object) -> None:
    """Every "no identity" shape answers None. None of them is an error.

    An anonymous visitor is an ordinary case this server answers with
    ``sign_in_required``, so the reader must not raise on any of these.
    """
    assert bearer_token_from_meta(ctx) is None, description


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="explicit-null"),
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_blank_or_null_token_is_treated_as_absent(value: object) -> None:
    """🔴 The case that protects the ``sign_in_required`` branch.

    The orchestrator drops falsy values when building ``_meta``, so an anonymous
    caller sends no key at all. If that filter is ever removed or changed, an
    explicit null or empty string would arrive instead — and a reader that returned
    it as-is would hand every tool a "signed-in" caller with no usable credential.
    Every blank shape has to collapse to the same None as a missing key.
    """
    ctx = _FakeCtx(_FakeMeta({META_TOKEN_KEY: value}))
    assert bearer_token_from_meta(ctx) is None


@pytest.mark.parametrize("value", [123, 12.5, True, ["a"], {"a": 1}])
def test_non_string_token_is_rejected(value: object) -> None:
    """A non-string is a caller bug, not an identity.

    Refusing it here keeps the return type honest, instead of letting an int reach
    an Authorization header and fail far away with an unrelated-looking message.
    """
    ctx = _FakeCtx(_FakeMeta({META_TOKEN_KEY: value}))
    assert bearer_token_from_meta(ctx) is None


def test_meta_key_name_is_the_orchestrator_contract() -> None:
    """Pin the key name; it is a cross-repo contract with no build to break it.

    The orchestrator writes this exact string in ``capability_executor.py``'s ``_meta``
    build (step O3 / VA-386). A rename on either side fails no build and no type
    check — it silently makes every caller look anonymous. This assertion is the
    cheapest place to notice.
    """
    assert META_TOKEN_KEY == "guestsite_bearer_token"
