"""Credential schemes: the port of Vessel's provider auth (providers.ts).

Config names the env var that holds a key. It never holds the key itself.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from vessel_mind.core.credentials import (
    BearerAuth,
    CredentialError,
    HeaderAuth,
    NoAuth,
    ProviderAuth,
    resolve_credential,
)

KEY = "AIza-test-secret-value"
auth_adapter: TypeAdapter[ProviderAuth] = TypeAdapter(ProviderAuth)


def test_the_three_schemes_parse_from_config():
    assert auth_adapter.validate_python({"scheme": "none"}) == NoAuth()
    assert auth_adapter.validate_python(
        {"scheme": "bearer", "key_env": "GEMINI_API_KEY"}
    ) == BearerAuth(key_env="GEMINI_API_KEY")
    assert auth_adapter.validate_python(
        {"scheme": "header", "header_name": "x-goog-api-key", "key_env": "GEMINI_API_KEY"}
    ) == HeaderAuth(header_name="x-goog-api-key", key_env="GEMINI_API_KEY")


@pytest.mark.parametrize(
    "key_env",
    [KEY, "gemini_api_key", "1KEY", "GEMINI API KEY", ""],
)
def test_key_env_must_be_an_env_var_name_so_a_pasted_key_fails(key_env):
    with pytest.raises(ValidationError) as error:
        BearerAuth.model_validate({"key_env": key_env})

    assert "NAME of an environment variable" in str(error.value)
    assert KEY not in str(error.value)


@pytest.mark.parametrize("header", ["Authorization", "authorization", "bad header", ""])
def test_a_header_scheme_refuses_authorization_and_invalid_names(header):
    with pytest.raises(ValidationError):
        auth_adapter.validate_python(
            {"scheme": "header", "header_name": header, "key_env": "GEMINI_API_KEY"}
        )


def test_a_scheme_is_required_and_a_raw_key_field_is_refused():
    with pytest.raises(ValidationError):
        auth_adapter.validate_python({"key_env": "GEMINI_API_KEY"})
    with pytest.raises(ValidationError):
        auth_adapter.validate_python({"scheme": "bearer", "key_env": "K", "key": KEY})


def test_bearer_resolves_to_the_authorization_header():
    credential = resolve_credential(BearerAuth(key_env="GEMINI_API_KEY"), {"GEMINI_API_KEY": KEY})

    assert credential is not None
    assert credential.headers() == {"Authorization": f"Bearer {KEY}"}


def test_header_resolves_to_the_named_header_with_the_trimmed_key():
    credential = resolve_credential(
        HeaderAuth(header_name="x-goog-api-key", key_env="GEMINI_API_KEY"),
        {"GEMINI_API_KEY": f"  {KEY}\n"},
    )

    assert credential is not None
    assert credential.headers() == {"x-goog-api-key": KEY}


def test_none_resolves_to_no_credential():
    assert resolve_credential(NoAuth(), {}) is None


@pytest.mark.parametrize("environ", [{}, {"GEMINI_API_KEY": "   "}])
def test_an_unset_or_empty_env_var_is_named_in_the_error(environ):
    with pytest.raises(CredentialError, match="GEMINI_API_KEY is unset or empty"):
        resolve_credential(BearerAuth(key_env="GEMINI_API_KEY"), environ)


def test_a_credential_never_shows_its_key():
    credential = resolve_credential(BearerAuth(key_env="GEMINI_API_KEY"), {"GEMINI_API_KEY": KEY})

    assert KEY not in repr(credential)
    assert KEY not in str(credential)
