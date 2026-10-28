"""FastAPI endpoints for Role-Based Profile Viewing, Editing, and Mobile Change Verification."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.api import (
    get_current_resolved_session,
    get_otp_service,
)
from app.modules.authentication.models import normalize_mobile_e164
from app.modules.authentication.otp_service import (
    MessagingProviderUnavailable,
    OtpRateLimitExceeded,
    OtpRateLimited,
    OtpService,
    OtpVerificationFailed,
)
from app.modules.authentication.repository import ResolvedSession
from app.modules.profiles.schemas import (
    MobileChangeRequest,
    MobileChangeResponse,
    MobileChangeVerifyRequest,
    MobileChangeVerifyResponse,
    ProfileUpdateRequest,
    ProfileViewResponse,
)
from app.modules.profiles.service import ProfileService

router = APIRouter(prefix="/profile", tags=["profiles"])


@router.get(
    "/me",
    response_model=ProfileViewResponse,
    summary="Get current user's profile",
)
def get_my_profile(
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> ProfileViewResponse:
    caller, _ = session_data
    target_type, prof, user_op, mem, role = ProfileService.resolve_caller_profile(session, caller)
    return ProfileService.build_profile_response(
        session=session,
        caller=caller,
        target_type=target_type,
        profile_obj=prof,
        user_or_operator=user_op,
        membership=mem,
        role_code=role,
    )


@router.patch(
    "/me",
    response_model=ProfileViewResponse,
    summary="Update current user's profile",
)
def update_my_profile(
    body: ProfileUpdateRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> ProfileViewResponse:
    caller, _ = session_data
    target_type, prof, user_op, mem, role = ProfileService.resolve_caller_profile(session, caller)
    return ProfileService.update_profile(
        session=session,
        caller=caller,
        target_type=target_type,
        profile_obj=prof,
        user_or_operator=user_op,
        membership=mem,
        role_code=role,
        body=body,
        otp_service=otp_service,
    )


@router.get(
    "/{profile_id}",
    response_model=ProfileViewResponse,
    summary="Get profile of a subordinate or peer user",
)
def get_user_profile(
    profile_id: UUID,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
) -> ProfileViewResponse:
    caller, _ = session_data
    target_type, prof, user_op, mem, role = ProfileService.resolve_target(session, profile_id)

    if not ProfileService.can_view(session, caller, target_type, prof, role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "You are not authorized to view this profile."},
        )

    return ProfileService.build_profile_response(
        session=session,
        caller=caller,
        target_type=target_type,
        profile_obj=prof,
        user_or_operator=user_op,
        membership=mem,
        role_code=role,
    )


@router.patch(
    "/{profile_id}",
    response_model=ProfileViewResponse,
    summary="Update profile of a subordinate user",
)
def update_user_profile(
    profile_id: UUID,
    body: ProfileUpdateRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    session: Annotated[Session, Depends(get_db)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> ProfileViewResponse:
    caller, _ = session_data
    target_type, prof, user_op, mem, role = ProfileService.resolve_target(session, profile_id)

    return ProfileService.update_profile(
        session=session,
        caller=caller,
        target_type=target_type,
        profile_obj=prof,
        user_or_operator=user_op,
        membership=mem,
        role_code=role,
        body=body,
        otp_service=otp_service,
    )


@router.post(
    "/mobile-change/request",
    response_model=MobileChangeResponse,
    summary="Request OTP verification to change mobile number",
)
async def request_mobile_change_otp(
    body: MobileChangeRequest,
    session_data: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> MobileChangeResponse:
    caller, _ = session_data
    try:
        clean_mobile = normalize_mobile_e164(body.new_mobile_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_MOBILE", "message": str(exc)},
        )

    try:
        challenge_id, cooldown = await otp_service.request_otp(
            mobile_number=clean_mobile,
            purpose="profile_mobile_change",
            tenant_id=caller.tenant_id,
        )
        return MobileChangeResponse(
            status="accepted",
            challenge_id=challenge_id,
            cooldown_seconds=cooldown,
            message="Verification code dispatched to your new mobile number.",
        )
    except (OtpRateLimitExceeded, OtpRateLimited) as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMIT_EXCEEDED", "message": str(exc), "retry_after": getattr(exc, "retry_after_seconds", 60)},
        )
    except MessagingProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "MESSAGING_PROVIDER_UNAVAILABLE", "message": str(exc)},
        )


@router.post(
    "/mobile-change/verify",
    response_model=MobileChangeVerifyResponse,
    summary="Verify OTP code for mobile number change",
)
def verify_mobile_change_otp(
    body: MobileChangeVerifyRequest,
    _: Annotated[tuple[ResolvedSession, str], Depends(get_current_resolved_session)],
    otp_service: Annotated[OtpService, Depends(get_otp_service)],
) -> MobileChangeVerifyResponse:
    try:
        clean_mobile = normalize_mobile_e164(body.new_mobile_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_MOBILE", "message": str(exc)},
        )

    try:
        success, verification_token = otp_service.verify_otp(
            challenge_id=body.challenge_id,
            mobile_number=clean_mobile,
            otp=body.otp,
            purpose="profile_mobile_change",
        )
    except OtpVerificationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "OTP_VERIFICATION_FAILED", "message": str(exc), "attempts_remaining": exc.attempts_remaining},
        )

    if not success or not verification_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "OTP_VERIFICATION_FAILED", "message": "Verification failed."},
        )

    return MobileChangeVerifyResponse(
        status="verified",
        verification_token=verification_token,
        message="Mobile number verified successfully. You may now save your profile.",
    )

