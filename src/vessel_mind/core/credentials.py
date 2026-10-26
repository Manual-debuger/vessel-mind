"""Credential schemes: a port of Vessel's provider auth (providers.ts).

Config names the environment variable that holds a credential; it never holds the
credential. The name must look like an env var name, so a pasted key fails
validation at startup. The scheme is required: "this endpoint needs no
credential" is a claim the config makes out loud, with scheme `none`.
"""

import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


def _env_var_name(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        # The value is not echoed: it may be a pasted key.
        raise ValueError(
            "must be the NAME of an environment variable in upper snake case, never the key itself"
        )
    return value


EnvVarName = Annotated[str, StringConstraints(strip_whitespace=True), AfterValidator(_env_var_name)]


def _header_name(value: str) -> str:
    if value.lower() == "authorization":
        raise ValueError("use scheme 'bearer' for the authorization header")
    return value


# RFC 7230 token characters. `authorization` is excluded: a bearer credential then
# has exactly one spelling, so there is one code path to keep out of logs.
HeaderName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$"),
    AfterValidator(_header_name),
]


class _Auth(BaseModel):
    # A validation error never repeats the input: the input may be a pasted key.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class NoAuth(_Auth):
    scheme: Literal["none"] = "none"


class BearerAuth(_Auth):
    scheme: Literal["bearer"] = "bearer"
    key_env: EnvVarName


class HeaderAuth(_Auth):
    scheme: Literal["header"] = "header"
    header_name: HeaderName
    key_env: EnvVarName


ProviderAuth = Annotated[NoAuth | BearerAuth | HeaderAuth, Field(discriminator="scheme")]


class CredentialError(Exception):
    """The env var a scheme names is unset or empty. The message names it, never a value."""


class Credential:
    """A resolved credential. Its value appears only in headers(), never in repr or str."""

    __slots__ = ("_header_name", "_value")

    def __init__(self, header_name: str, value: str) -> None:
        self._header_name = header_name
        self._value = value

    def headers(self) -> dict[str, str]:
        return {self._header_name: self._value}

    def __repr__(self) -> str:
        return f"Credential(header={self._header_name!r}, value=<hidden>)"

    __str__ = __repr__


def resolve_credential(auth: ProviderAuth, environ: Mapping[str, str]) -> Credential | None:
    if isinstance(auth, NoAuth):
        return None
    key = (environ.get(auth.key_env) or "").strip()
    if not key:
        raise CredentialError(f"{auth.key_env} is unset or empty")
    if isinstance(auth, BearerAuth):
        return Credential("Authorization", f"Bearer {key}")
    return Credential(auth.header_name, key)
