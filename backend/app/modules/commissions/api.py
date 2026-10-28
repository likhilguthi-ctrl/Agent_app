from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.modules.authentication.repository import ResolvedSession
from app.modules.authorization.catalog import PermissionCode
from app.modules.authorization.dependencies import require_session_capability
from app.modules.commissions.models import CommissionAccount, CommissionJournal, CommissionLedgerEntry
from app.modules.commissions.schemas import (
    CommissionAccountSummary,
    JournalCreateRequest,
    JournalDetailResponse,
    JournalReversalRequest,
    JournalSummary,
    LedgerEntriesListResponse,
    LedgerEntrySummary,
)
from app.modules.commissions.service import (
    FinancialLedgerService,
    get_financial_ledger_service,
)

router = APIRouter(prefix="/commissions", tags=["commissions"])


def _to_account_summary(acct: CommissionAccount) -> CommissionAccountSummary:
    return CommissionAccountSummary(
        id=acct.id,
        tenant_id=acct.tenant_id,
        agent_membership_id=acct.agent_membership_id,
        agent_profile_id=acct.agent_profile_id,
        account_type=acct.account_type,
        account_number=acct.account_number,
        currency=acct.currency,
        current_balance=acct.current_balance,
        status=acct.status,
        allow_negative=acct.allow_negative,
        created_at=acct.created_at,
        updated_at=acct.updated_at,
    )


def _to_journal_summary(j: CommissionJournal) -> JournalSummary:
    return JournalSummary(
        id=j.id,
        tenant_id=j.tenant_id,
        journal_number=j.journal_number,
        description=j.description,
        reference_type=j.reference_type,
        reference_id=j.reference_id,
        idempotency_key=j.idempotency_key,
        currency=j.currency,
        total_amount=j.total_amount,
        status=j.status,
        posted_at=j.posted_at,
        created_by_user_id=j.created_by_user_id,
        created_at=j.created_at,
    )


def _to_ledger_summary(e: CommissionLedgerEntry) -> LedgerEntrySummary:
    return LedgerEntrySummary(
        id=e.id,
        tenant_id=e.tenant_id,
        journal_id=e.journal_id,
        account_id=e.account_id,
        entry_type=e.entry_type,
        amount=e.amount,
        currency=e.currency,
        balance_after=e.balance_after,
        description=e.description,
        created_at=e.created_at,
    )


# -----------------------------------------------------------------------------
# 1. Accounts Endpoints
# -----------------------------------------------------------------------------
@router.get(
    "/accounts/me",
    response_model=CommissionAccountSummary,
    summary="Get commission account of the currently authenticated agent",
)
def get_current_agent_commission_account(
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
) -> CommissionAccountSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    acct = service.get_agent_account_by_user_id(
        session,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
    )
    return _to_account_summary(acct)


@router.get(
    "/accounts/{account_id}",
    response_model=CommissionAccountSummary,
    summary="Get financial account by ID within the community",
)
def get_commission_account_by_id(
    account_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
) -> CommissionAccountSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    acct = service.get_account_by_id(
        session,
        tenant_id=caller.tenant_id,
        account_id=account_id,
    )
    return _to_account_summary(acct)


@router.get(
    "/accounts/{account_id}/entries",
    response_model=LedgerEntriesListResponse,
    summary="List ledger entries for an account with pagination",
)
def list_account_ledger_entries(
    account_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> LedgerEntriesListResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    entries, total = service.list_account_entries(
        session,
        tenant_id=caller.tenant_id,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )
    items = [_to_ledger_summary(e) for e in entries]
    return LedgerEntriesListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# -----------------------------------------------------------------------------
# 2. Journals & Double-Entry Ledger Endpoints
# -----------------------------------------------------------------------------
@router.post(
    "/journals",
    response_model=JournalSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Post an atomic double-entry financial journal",
)
def post_financial_journal(
    body: JournalCreateRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
) -> JournalSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    journal = service.post_journal(
        session,
        tenant_id=caller.tenant_id,
        request=body,
        created_by_user_id=caller.user_id,
    )
    session.commit()
    return _to_journal_summary(journal)


@router.get(
    "/journals/{journal_id}",
    response_model=JournalDetailResponse,
    summary="Get journal details and its ledger line items",
)
def get_journal_detail(
    journal_id: UUID,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_READ))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
) -> JournalDetailResponse:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    journal, entries = service.get_journal_detail(
        session,
        tenant_id=caller.tenant_id,
        journal_id=journal_id,
    )
    return JournalDetailResponse(
        journal=_to_journal_summary(journal),
        entries=[_to_ledger_summary(e) for e in entries],
    )


@router.post(
    "/journals/{journal_id}/reversal",
    response_model=JournalSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Post a compensating reversal for an existing posted journal",
)
def reverse_financial_journal(
    journal_id: UUID,
    body: JournalReversalRequest,
    caller: Annotated[ResolvedSession, Depends(require_session_capability(PermissionCode.COMMISSION_MANAGE))],
    session: Annotated[Session, Depends(get_db)],
    service: Annotated[FinancialLedgerService, Depends(get_financial_ledger_service)],
) -> JournalSummary:
    if not caller.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "NO_COMMUNITY_CONTEXT", "message": "Caller has no active community context."},
        )

    reversal_journal = service.reverse_journal(
        session,
        tenant_id=caller.tenant_id,
        original_journal_id=journal_id,
        reason=body.reason,
        created_by_user_id=caller.user_id,
        idempotency_key=body.idempotency_key,
    )
    session.commit()
    return _to_journal_summary(reversal_journal)

