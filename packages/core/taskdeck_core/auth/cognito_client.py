"""Async wrapper around boto3's ``cognito-idp`` client.

boto3 is sync; we run each call in a threadpool. Errors are mapped from
``botocore.exceptions.ClientError`` to typed exceptions so the API layer
can return clean HTTP statuses without leaking AWS error code strings.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


# ── Typed errors ─────────────────────────────────────────────────────────


class CognitoError(Exception):
    """Base for all auth-flow errors. Carries the AWS code for logging."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class InvalidCredentials(CognitoError): ...
class UserNotConfirmed(CognitoError): ...
class CodeMismatch(CognitoError): ...
class ExpiredCode(CognitoError): ...
class LimitExceeded(CognitoError): ...
class TooManyRequests(CognitoError): ...
class UserNotFound(CognitoError): ...
class UsernameExists(CognitoError): ...
class InvalidPassword(CognitoError): ...
class UnsupportedChallenge(CognitoError): ...
class CognitoUnavailable(CognitoError): ...


_ERROR_MAP: dict[str, type[CognitoError]] = {
    "NotAuthorizedException": InvalidCredentials,
    "UserNotConfirmedException": UserNotConfirmed,
    "CodeMismatchException": CodeMismatch,
    "ExpiredCodeException": ExpiredCode,
    "LimitExceededException": LimitExceeded,
    "TooManyRequestsException": TooManyRequests,
    "UserNotFoundException": UserNotFound,
    "UsernameExistsException": UsernameExists,
    "InvalidPasswordException": InvalidPassword,
    "InvalidParameterException": InvalidPassword,  # Cognito uses this for weak pw too
}


def _map_error(e: ClientError) -> CognitoError:
    code = e.response.get("Error", {}).get("Code", "Unknown")
    message = e.response.get("Error", {}).get("Message", str(e))
    cls = _ERROR_MAP.get(code, CognitoUnavailable)
    return cls(code, message)


# ── Async client ─────────────────────────────────────────────────────────


class CognitoClient:
    """Thin async facade over ``boto3.client('cognito-idp')``.

    All methods translate ``ClientError`` into typed ``CognitoError``
    subclasses. Methods accepting a ``Session`` parameter (the long
    opaque blob Cognito returns between challenge rounds) propagate it
    untouched — the caller is responsible for storing it in the
    ``FlowStore`` keyed by ``flow_id``.
    """

    def __init__(self, *, region: str, client_id: str, user_pool_id: str | None = None) -> None:
        self._client = boto3.client("cognito-idp", region_name=region)
        self._client_id = client_id
        self._user_pool_id = user_pool_id

    @property
    def client_id(self) -> str:
        return self._client_id

    async def _run(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except ClientError as e:
            mapped = _map_error(e)
            log.info("cognito error: %s", mapped)
            raise mapped from e

    # ── auth flow ────────────────────────────────────────────────────

    async def initiate_auth_srp(self, *, username: str, srp_a: str) -> dict[str, Any]:
        return await self._run(
            self._client.initiate_auth,
            ClientId=self._client_id,
            AuthFlow="USER_SRP_AUTH",
            AuthParameters={"USERNAME": username, "SRP_A": srp_a},
        )

    async def respond_password_verifier(
        self,
        *,
        username: str,
        password_proof: str,
        timestamp: str,
        secret_block: str,
        session: str | None,
    ) -> dict[str, Any]:
        # Cognito does NOT return a Session on the initial PASSWORD_VERIFIER
        # roundtrip. Only later challenges (MFA, NEW_PASSWORD) carry one.
        # boto3 rejects Session="" / Session=None — drop the kwarg instead.
        kwargs: dict[str, Any] = {
            "ClientId": self._client_id,
            "ChallengeName": "PASSWORD_VERIFIER",
            "ChallengeResponses": {
                "USERNAME": username,
                "PASSWORD_CLAIM_SIGNATURE": password_proof,
                "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
                "TIMESTAMP": timestamp,
            },
        }
        if session:
            kwargs["Session"] = session
        return await self._run(self._client.respond_to_auth_challenge, **kwargs)

    async def respond_software_token_mfa(
        self, *, username: str, code: str, session: str
    ) -> dict[str, Any]:
        return await self._run(
            self._client.respond_to_auth_challenge,
            ClientId=self._client_id,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=session,
            ChallengeResponses={
                "USERNAME": username,
                "SOFTWARE_TOKEN_MFA_CODE": code,
            },
        )

    async def respond_mfa_setup(
        self, *, username: str, session: str
    ) -> dict[str, Any]:
        return await self._run(
            self._client.respond_to_auth_challenge,
            ClientId=self._client_id,
            ChallengeName="MFA_SETUP",
            Session=session,
            ChallengeResponses={"USERNAME": username},
        )

    async def respond_new_password(
        self,
        *,
        username: str,
        new_password: str,
        session: str,
        attrs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        responses = {"USERNAME": username, "NEW_PASSWORD": new_password}
        if attrs:
            for k, v in attrs.items():
                responses[f"userAttributes.{k}"] = v
        return await self._run(
            self._client.respond_to_auth_challenge,
            ClientId=self._client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=session,
            ChallengeResponses=responses,
        )

    async def associate_software_token(self, *, session: str) -> dict[str, Any]:
        return await self._run(
            self._client.associate_software_token, Session=session
        )

    async def verify_software_token(
        self, *, session: str, code: str, friendly_name: str
    ) -> dict[str, Any]:
        return await self._run(
            self._client.verify_software_token,
            Session=session,
            UserCode=code,
            FriendlyDeviceName=friendly_name,
        )

    # ── token lifecycle ──────────────────────────────────────────────

    async def refresh_tokens(self, *, refresh_token: str) -> dict[str, Any]:
        return await self._run(
            self._client.initiate_auth,
            ClientId=self._client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )

    async def global_sign_out(self, *, access_token: str) -> None:
        await self._run(self._client.global_sign_out, AccessToken=access_token)

    # ── signup ───────────────────────────────────────────────────────

    async def sign_up(
        self, *, username: str, password: str, email: str
    ) -> dict[str, Any]:
        return await self._run(
            self._client.sign_up,
            ClientId=self._client_id,
            Username=username,
            Password=password,
            UserAttributes=[{"Name": "email", "Value": email}],
        )

    async def confirm_sign_up(self, *, username: str, code: str) -> None:
        await self._run(
            self._client.confirm_sign_up,
            ClientId=self._client_id,
            Username=username,
            ConfirmationCode=code,
        )

    async def resend_confirmation_code(self, *, username: str) -> None:
        await self._run(
            self._client.resend_confirmation_code,
            ClientId=self._client_id,
            Username=username,
        )

    # ── password reset ────────────────────────────────────────────────

    async def forgot_password(self, *, username: str) -> dict[str, Any]:
        return await self._run(
            self._client.forgot_password,
            ClientId=self._client_id,
            Username=username,
        )

    async def confirm_forgot_password(
        self, *, username: str, code: str, new_password: str
    ) -> None:
        await self._run(
            self._client.confirm_forgot_password,
            ClientId=self._client_id,
            Username=username,
            ConfirmationCode=code,
            Password=new_password,
        )

    # ── inspection ───────────────────────────────────────────────────

    async def get_user(self, *, access_token: str) -> dict[str, Any]:
        return await self._run(self._client.get_user, AccessToken=access_token)
