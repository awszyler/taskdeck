"""BFF auth endpoints (P5.1).

The browser POSTs structured JSON; we proxy to Cognito via boto3 and
return the next step of the SRP / MFA state machine. On a successful
``AuthenticationResult`` we encrypt the tokens, INSERT a ``user_sessions``
row, and Set-Cookie an opaque UUID. Cognito's own session/JWT material
never leaves the backend process.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from taskdeck_core.auth.cognito_client import (
    CodeMismatch,
    CognitoError,
    InvalidCredentials,
    InvalidPassword,
    LimitExceeded,
    UnsupportedChallenge,
    UsernameExists,
    UserNotFound,
)
from taskdeck_core.auth.flow_store import InMemoryFlowStore
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal
from taskdeck_core.auth.session import (
    create_session,
    revoke_session,
    upsert_user_from_claims,
)
from taskdeck_core.db.models import User, UserSession
from taskdeck_core.hardening.rate_limit import limiter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Schemas ──────────────────────────────────────────────────────────────


class AuthConfigOut(BaseModel):
    auth_mode: Literal["disabled", "cognito"]
    allow_signup: bool
    # SRP namespace — the part of the User Pool ID after the underscore.
    # NOT the User Pool ID; this is just the group identifier the SRP
    # info-string needs to match what Cognito uses server-side. Knowing
    # it does not let an attacker do anything useful (no client_id,
    # no client_secret, no AWS account info). Only set in cognito mode.
    cognito_pool_name: str | None = None


class MeOut(BaseModel):
    id: str
    login: str | None
    name: str | None
    avatar_url: str | None
    email: str | None = None


class LoginInitIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    srp_a: str = Field(min_length=8, max_length=4096)


class LoginInitOut(BaseModel):
    flow_id: str
    srp_b: str
    salt: str
    secret_block: str
    username_internal: str  # Cognito's internal username (== sub for email-pool)


class LoginRespondIn(BaseModel):
    flow_id: str
    password_proof: str
    timestamp: str
    secret_block: str


class LoginTotpIn(BaseModel):
    flow_id: str
    code: str = Field(min_length=4, max_length=10)


class LoginMfaSetupIn(BaseModel):
    flow_id: str
    code: str = Field(min_length=4, max_length=10)
    friendly_device_name: str = Field(min_length=1, max_length=64)


class LoginNewPasswordIn(BaseModel):
    flow_id: str
    new_password: str = Field(min_length=8, max_length=256)


class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class SignupConfirmIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=10)


class ResendConfirmIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ForgotPasswordIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetPasswordIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=10)
    new_password: str = Field(min_length=8, max_length=256)


# ── Helpers ──────────────────────────────────────────────────────────────


def _flow_store(request: Request) -> InMemoryFlowStore:
    fs = getattr(request.app.state, "flow_store", None)
    if fs is None:
        raise HTTPException(500, "flow store not configured")
    return fs


def _require_cognito(request: Request) -> Any:
    settings = request.app.state.settings
    if settings.auth_mode != "cognito":
        raise HTTPException(400, "auth not enabled")
    return request.app.state.cognito_client


def _set_session_cookie(resp: Response, *, sid: UUID, settings: Any) -> None:
    domain = settings.session_cookie_domain or None
    resp.set_cookie(
        settings.session_cookie_name,
        str(sid),
        max_age=30 * 86400,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        domain=domain,
    )


def _build_otpauth_uri(*, secret: str, email: str) -> str:
    issuer = "Taskdeck"
    label = quote(f"{issuer}:{email}", safe="")
    secret_q = quote(secret, safe="")
    return f"otpauth://totp/{label}?secret={secret_q}&issuer={quote(issuer, safe='')}"


async def _challenge_response(
    *,
    request: Request,
    flow_id: str,
    cognito: Any,
    flow_state: dict[str, Any],
    cognito_resp: dict[str, Any],
    response: Response,
) -> dict[str, Any]:
    """Map a Cognito respond_to_auth_challenge result onto the next
    backend HTTP shape."""
    auth = cognito_resp.get("AuthenticationResult")
    if auth is not None:
        await _finalise_login(
            request=request,
            cognito=cognito,
            cognito_sub_hint=flow_state.get("username_internal"),
            email=flow_state["email"],
            tokens=auth,
            response=response,
        )
        await _flow_store(request).delete(flow_id)
        return {"status": "ok"}

    challenge = cognito_resp.get("ChallengeName")
    session = cognito_resp.get("Session")
    flow_state["session"] = session

    if challenge == "MFA_SETUP":
        assoc = await cognito.associate_software_token(session=session)
        secret = assoc["SecretCode"]
        flow_state["session"] = assoc.get("Session", session)
        await _flow_store(request).put(flow_id, flow_state)
        return {
            "status": "mfa_setup",
            "flow_id": flow_id,
            "otpauth_uri": _build_otpauth_uri(
                secret=secret, email=flow_state["email"]
            ),
            "secret": secret,
        }
    if challenge == "SOFTWARE_TOKEN_MFA":
        await _flow_store(request).put(flow_id, flow_state)
        return {"status": "totp_required", "flow_id": flow_id}
    if challenge == "NEW_PASSWORD_REQUIRED":
        await _flow_store(request).put(flow_id, flow_state)
        params = cognito_resp.get("ChallengeParameters", {}) or {}
        required = params.get("requiredAttributes", "[]")
        return {
            "status": "new_password_required",
            "flow_id": flow_id,
            "required_attributes": required,
        }
    raise UnsupportedChallenge(challenge or "Unknown", "unsupported challenge")


async def _finalise_login(
    *,
    request: Request,
    cognito: Any,
    cognito_sub_hint: str | None,
    email: str,
    tokens: dict[str, Any],
    response: Response,
) -> None:
    access = tokens["AccessToken"]
    refresh = tokens["RefreshToken"]
    expires_in = int(tokens.get("ExpiresIn") or 3600)

    settings = request.app.state.settings
    fernet = request.app.state.fernet
    sm = request.app.state.db_sessionmaker

    # Pull the canonical sub from the access token (more reliable than
    # what Cognito reports as the username, which for email pools is
    # already the sub but stay defensive).
    user_info = await cognito.get_user(access_token=access)
    sub_v: str | None = None
    name: str | None = None
    user_email: str = email
    for attr in user_info.get("UserAttributes", []) or []:
        if attr["Name"] == "sub":
            sub_v = attr["Value"]
        elif attr["Name"] == "email":
            user_email = attr["Value"]
        elif attr["Name"] == "name":
            name = attr["Value"]
    sub: str = (
        sub_v
        or cognito_sub_hint
        or str(user_info.get("Username") or email)
    )

    async with sm() as db:
        user = await upsert_user_from_claims(
            db, cognito_sub=sub, email=user_email, name=name
        )
        await db.flush()
        sess_row = await create_session(
            db,
            fernet=fernet,
            user_id=user.id,
            cognito_sub=sub,
            access_token=access,
            access_token_ttl_seconds=expires_in,
            refresh_token=refresh,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        await db.commit()
        sid = sess_row.id

    _set_session_cookie(response, sid=sid, settings=settings)


# ── Public config ────────────────────────────────────────────────────────


@router.get("/config", response_model=AuthConfigOut)
async def auth_config(request: Request) -> AuthConfigOut:
    s = request.app.state.settings
    pool_name: str | None = None
    if s.auth_mode == "cognito" and s.cognito_user_pool_id:
        pool_name = (
            s.cognito_user_pool_id.split("_", 1)[1]
            if "_" in s.cognito_user_pool_id
            else s.cognito_user_pool_id
        )
    return AuthConfigOut(
        auth_mode=s.auth_mode,
        allow_signup=bool(s.auth_allow_signup) if s.auth_mode == "cognito" else False,
        cognito_pool_name=pool_name,
    )


@router.get("/me", response_model=MeOut)
@limiter.limit("60/minute")
async def me(request: Request) -> MeOut:
    settings = request.app.state.settings
    principal = await current_principal(request)
    if isinstance(principal, ServicePrincipal):
        if settings.auth_mode == "disabled":
            return MeOut(
                id="00000000-0000-0000-0000-000000000000",
                login="local",
                name="Local user",
                avatar_url=None,
                email=None,
            )
        raise HTTPException(401, "not a user session")
    u: User = principal
    return MeOut(
        id=str(u.id),
        login=u.login,
        name=u.name,
        avatar_url=u.avatar_url,
        email=u.email,
    )


# ── Login flow ───────────────────────────────────────────────────────────


@router.post("/login/init", response_model=LoginInitOut)
@limiter.limit("10/minute")
async def login_init(request: Request, body: LoginInitIn) -> LoginInitOut:
    cognito = _require_cognito(request)
    try:
        out = await cognito.initiate_auth_srp(
            username=body.email, srp_a=body.srp_a
        )
    except (UserNotFound, InvalidCredentials):
        # Generic message to avoid email enumeration — but the SRP
        # exchange itself can leak this through timing if we want to be
        # paranoid. Acceptable for v1.
        raise HTTPException(401, "invalid credentials") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth temporarily unavailable: {e.code}") from e

    if out.get("ChallengeName") != "PASSWORD_VERIFIER":
        raise HTTPException(500, f"unexpected challenge: {out.get('ChallengeName')}")
    params = out["ChallengeParameters"]

    # Cognito's "username" the client must use for SRP HKDF + signature.
    # USER_ID_FOR_SRP is the canonical name (== USERNAME for email pools,
    # but the SDK hot-swaps to USER_ID_FOR_SRP and tests prove that's the
    # value HMAC'd into the signature input).
    user_id_for_srp = params.get("USER_ID_FOR_SRP") or params["USERNAME"]

    flow_id = InMemoryFlowStore.new_flow_id()
    state = {
        "step": "password_verifier",
        "email": body.email,
        # Cognito does NOT return a top-level Session on the initial
        # PASSWORD_VERIFIER challenge — it expects the next call to
        # respond with the proof and no Session. Treat absent as None
        # so the cognito_client wrapper omits the Session parameter
        # rather than passing an empty string.
        "session": out.get("Session"),
        "username_internal": user_id_for_srp,
    }
    await _flow_store(request).put(flow_id, state)

    return LoginInitOut(
        flow_id=flow_id,
        srp_b=params["SRP_B"],
        salt=params["SALT"],
        secret_block=params["SECRET_BLOCK"],
        username_internal=user_id_for_srp,
    )


@router.post("/login/respond")
@limiter.limit("10/minute")
async def login_respond(
    request: Request, body: LoginRespondIn, response: Response
) -> dict[str, Any]:
    state = await _flow_store(request).get(body.flow_id)
    if state is None or state.get("step") != "password_verifier":
        raise HTTPException(400, "unknown or expired flow")
    cognito = _require_cognito(request)
    try:
        out = await cognito.respond_password_verifier(
            username=state["username_internal"],
            password_proof=body.password_proof,
            timestamp=body.timestamp,
            secret_block=body.secret_block,
            session=state["session"],
        )
    except InvalidCredentials:
        raise HTTPException(401, "invalid credentials") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e

    return await _challenge_response(
        request=request,
        flow_id=body.flow_id,
        cognito=cognito,
        flow_state=state,
        cognito_resp=out,
        response=response,
    )


@router.post("/login/totp")
@limiter.limit("10/minute")
async def login_totp(
    request: Request, body: LoginTotpIn, response: Response
) -> dict[str, Any]:
    state = await _flow_store(request).get(body.flow_id)
    if state is None or state.get("session") is None:
        raise HTTPException(400, "unknown or expired flow")
    cognito = _require_cognito(request)
    try:
        out = await cognito.respond_software_token_mfa(
            username=state["username_internal"],
            code=body.code,
            session=state["session"],
        )
    except CodeMismatch:
        raise HTTPException(401, "incorrect code") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e
    return await _challenge_response(
        request=request,
        flow_id=body.flow_id,
        cognito=cognito,
        flow_state=state,
        cognito_resp=out,
        response=response,
    )


@router.post("/login/mfa-setup")
@limiter.limit("10/minute")
async def login_mfa_setup(
    request: Request, body: LoginMfaSetupIn, response: Response
) -> dict[str, Any]:
    state = await _flow_store(request).get(body.flow_id)
    if state is None or state.get("session") is None:
        raise HTTPException(400, "unknown or expired flow")
    cognito = _require_cognito(request)
    # 1. Verify the code attaches the authenticator to the user.
    try:
        verify_out = await cognito.verify_software_token(
            session=state["session"],
            code=body.code,
            friendly_name=body.friendly_device_name,
        )
    except CodeMismatch:
        raise HTTPException(401, "incorrect code") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e

    if verify_out.get("Status") != "SUCCESS":
        raise HTTPException(401, "totp verification failed")

    # 2. Tell Cognito the MFA setup challenge is satisfied — this is
    #    what unlocks the AuthenticationResult.
    try:
        out = await cognito.respond_mfa_setup(
            username=state["username_internal"],
            session=verify_out.get("Session", state["session"]),
        )
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e

    return await _challenge_response(
        request=request,
        flow_id=body.flow_id,
        cognito=cognito,
        flow_state=state,
        cognito_resp=out,
        response=response,
    )


@router.post("/login/new-password")
@limiter.limit("10/minute")
async def login_new_password(
    request: Request, body: LoginNewPasswordIn, response: Response
) -> dict[str, Any]:
    state = await _flow_store(request).get(body.flow_id)
    if state is None or state.get("session") is None:
        raise HTTPException(400, "unknown or expired flow")
    cognito = _require_cognito(request)
    try:
        out = await cognito.respond_new_password(
            username=state["username_internal"],
            new_password=body.new_password,
            session=state["session"],
        )
    except InvalidPassword as e:
        raise HTTPException(400, f"password rejected: {e.message}") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e
    return await _challenge_response(
        request=request,
        flow_id=body.flow_id,
        cognito=cognito,
        flow_state=state,
        cognito_resp=out,
        response=response,
    )


# ── Signup (gated) ───────────────────────────────────────────────────────


def _signup_allowed(request: Request) -> None:
    s = request.app.state.settings
    if s.auth_mode != "cognito":
        raise HTTPException(400, "auth not enabled")
    if not s.auth_allow_signup:
        raise HTTPException(403, "signup disabled")


@router.post("/signup")
@limiter.limit("3/minute")
async def signup(request: Request, body: SignupIn) -> dict[str, Any]:
    _signup_allowed(request)
    cognito = _require_cognito(request)
    try:
        out = await cognito.sign_up(
            username=body.email, password=body.password, email=body.email
        )
    except UsernameExists:
        # Don't leak — same shape success-side; client does email-confirm
        # flow which would fail with CodeMismatch on resend, so user gets
        # the typical "check your email" UX with no enumeration.
        return {"status": "ok"}
    except InvalidPassword as e:
        raise HTTPException(400, f"password rejected: {e.message}") from None
    except LimitExceeded:
        raise HTTPException(429, "too many signup attempts") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e
    return {"status": "ok", "user_sub": out.get("UserSub")}


@router.post("/signup/confirm")
@limiter.limit("10/minute")
async def signup_confirm(request: Request, body: SignupConfirmIn) -> dict[str, str]:
    _signup_allowed(request)
    cognito = _require_cognito(request)
    try:
        await cognito.confirm_sign_up(username=body.email, code=body.code)
    except CodeMismatch:
        raise HTTPException(401, "incorrect code") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e
    return {"status": "ok"}


@router.post("/signup/resend")
@limiter.limit("3/minute")
async def signup_resend(request: Request, body: ResendConfirmIn) -> dict[str, str]:
    _signup_allowed(request)
    cognito = _require_cognito(request)
    # Stay silent on Cognito errors to avoid enumeration.
    with contextlib.suppress(CognitoError):
        await cognito.resend_confirmation_code(username=body.email)
    return {"status": "ok"}


# ── Password reset ───────────────────────────────────────────────────────


@router.post("/password/forgot")
@limiter.limit("3/minute")
async def password_forgot(
    request: Request, body: ForgotPasswordIn
) -> dict[str, str]:
    cognito = _require_cognito(request)
    # Return ok regardless — prevents email enumeration.
    with contextlib.suppress(CognitoError):
        await cognito.forgot_password(username=body.email)
    return {"status": "ok"}


@router.post("/password/reset")
@limiter.limit("10/minute")
async def password_reset(
    request: Request, body: ResetPasswordIn
) -> dict[str, str]:
    cognito = _require_cognito(request)
    try:
        await cognito.confirm_forgot_password(
            username=body.email, code=body.code, new_password=body.new_password
        )
    except CodeMismatch:
        raise HTTPException(401, "incorrect code") from None
    except InvalidPassword as e:
        raise HTTPException(400, f"password rejected: {e.message}") from None
    except CognitoError as e:
        raise HTTPException(503, f"auth error: {e.code}") from e
    return {"status": "ok"}


# ── Logout ───────────────────────────────────────────────────────────────


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias="ccpt_session"),
) -> Response:
    settings = request.app.state.settings
    cookie_name = settings.session_cookie_name
    sid_str = request.cookies.get(cookie_name) or session_cookie
    if sid_str:
        try:
            sid = UUID(sid_str)
        except ValueError:
            sid = None
        if sid is not None and settings.auth_mode == "cognito":
            sm = request.app.state.db_sessionmaker
            fernet = request.app.state.fernet
            cognito = request.app.state.cognito_client
            async with sm() as db:
                row = await db.get(UserSession, sid)
                if row is not None:
                    await revoke_session(
                        db, sess_row=row, fernet=fernet, cognito=cognito
                    )
                    await db.commit()
    out = Response(status_code=204)
    out.delete_cookie(
        cookie_name,
        path="/",
        domain=settings.session_cookie_domain or None,
    )
    return out


