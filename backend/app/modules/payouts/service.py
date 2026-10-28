from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.agents.models import AgentProfile
from app.modules.audit.service import AuditEventDraft, AuditService
from app.modules.commissions.models import (
    AccountStatus,
    AccountType,
    CommissionAccount,
    CommissionJournal,
    EntryType,
    JournalReferenceType,
)
from app.modules.commissions.schemas import JournalCreateRequest, JournalEntryLineSpec
from app.modules.commissions.service import FinancialLedgerService, get_financial_ledger_service
from app.modules.identity.models import TenantMembership, User
from app.modules.payouts.models import PayoutRequest, PayoutStatus, TERMINAL_PAYOUT_STATUSES
from app.modules.payouts.schemas import (
    AgentEligibleBalanceResponse,
    PayoutCancelRequest,
    PayoutCreateRequest,
    PayoutDisburseRequest,
    PayoutFailRequest,
    PayoutReviewRequest,
    PayoutSummary,
)
from app.modules.platform_identity.models import PlatformOperator

logger = logging.getLogger(__name__)

MIN_PAYOUT_AMOUNT = Decimal("1.00")


class PayoutService:
    """Orchestrates agent payout requests, community admin review, and platform super admin disbursement."""

    def __init__(
        self,
        ledger_service: FinancialLedgerService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._audit = audit_service or AuditService()
        self._ledger = ledger_service or get_financial_ledger_service()

    def get_agent_eligible_balance(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        agent_membership_id: UUID,
    ) -> AgentEligibleBalanceResponse:
        """Calculates authoritative available-to-withdraw balance from immutable ledger."""
        mem = session.execute(
            select(TenantMembership).where(
                TenantMembership.id == agent_membership_id,
                TenantMembership.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if mem is None or mem.status != "active":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "AGENT_NOT_FOUND", "message": "Active agent membership not found."},
            )

        acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.agent_membership_id == agent_membership_id,
            )
        ).scalar_one_or_none()

        current_balance = acct.current_balance if acct else Decimal("0.00")
        currency = acct.currency if acct else "INR"

        # Calculate pending payout requests
        pending_sum = session.execute(
            select(func.coalesce(func.sum(PayoutRequest.amount), Decimal("0.00"))).where(
                PayoutRequest.tenant_id == tenant_id,
                PayoutRequest.agent_membership_id == agent_membership_id,
                PayoutRequest.status.in_([
                    PayoutStatus.PENDING_REVIEW.value,
                    PayoutStatus.APPROVED.value,
                    PayoutStatus.PROCESSING.value,
                ]),
            )
        ).scalar() or Decimal("0.00")

        # Because PAYOUT_HOLD moves funds to PAYOUT_CLEARING upon submission,
        # current_balance on AGENT_COMMISSION account is already net of pending holds!
        available = max(Decimal("0.00"), current_balance)

        return AgentEligibleBalanceResponse(
            agent_membership_id=agent_membership_id,
            currency=currency,
            current_balance=current_balance,
            pending_payout_amount=Decimal(str(pending_sum)),
            available_to_withdraw=available,
            minimum_withdrawal_amount=MIN_PAYOUT_AMOUNT,
        )

    def request_payout(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        caller_membership: TenantMembership,
        caller_user: User,
        request: PayoutCreateRequest,
    ) -> PayoutRequest:
        """Agent submits a payout request subject to authoritative balance check and escrow hold."""
        if caller_membership.status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "INACTIVE_MEMBERSHIP", "message": "Only active agents may request payouts."},
            )

        prof = session.execute(
            select(AgentProfile).where(
                AgentProfile.membership_id == caller_membership.id,
                AgentProfile.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if prof is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "AGENT_PROFILE_NOT_FOUND", "message": "Agent profile not found in this community."},
            )

        # Idempotency check
        clean_key = request.idempotency_key.strip()
        existing = session.execute(
            select(PayoutRequest).where(
                PayoutRequest.tenant_id == tenant_id,
                PayoutRequest.idempotency_key == clean_key,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if (
                existing.amount == request.amount
                and existing.agent_membership_id == caller_membership.id
            ):
                return existing
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key previously used with different payout request payload.",
                },
            )

        if request.amount < MIN_PAYOUT_AMOUNT:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "BELOW_MINIMUM_WITHDRAWAL",
                    "message": f"Requested amount ({request.amount}) is below the minimum threshold of ₹{MIN_PAYOUT_AMOUNT}.",
                },
            )

        # Row-level lock on agent commission account to prevent concurrent overdraft races
        acct = session.execute(
            select(CommissionAccount)
            .where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.agent_membership_id == caller_membership.id,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if acct is None or acct.current_balance < request.amount:
            curr_bal = acct.current_balance if acct else Decimal("0.00")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "INSUFFICIENT_BALANCE",
                    "message": f"Requested amount ({request.amount}) exceeds eligible balance ({curr_bal}).",
                },
            )

        if acct.status != AccountStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "ACCOUNT_INACTIVE", "message": "Commission account is not active."},
            )

        now = datetime.now(UTC)
        payout_id = uuid4()

        # Post PAYOUT_HOLD double-entry journal (Debit AGENT_COMMISSION, Credit PAYOUT_CLEARING)
        clearing_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=tenant_id,
            account_type=AccountType.PAYOUT_CLEARING,
            currency=acct.currency,
        )

        hold_req = JournalCreateRequest(
            idempotency_key=f"PAYOUT-HOLD-{payout_id}",
            description=f"Escrow hold for agent payout request {payout_id}",
            reference_type=JournalReferenceType.PAYOUT_HOLD.value,
            reference_id=str(payout_id),
            currency=acct.currency,
            entries=[
                JournalEntryLineSpec(
                    account_id=acct.id,
                    entry_type=EntryType.DEBIT.value,
                    amount=request.amount,
                    description=f"Hold for payout request {payout_id}",
                ),
                JournalEntryLineSpec(
                    account_id=clearing_acct.id,
                    entry_type=EntryType.CREDIT.value,
                    amount=request.amount,
                    description=f"Escrow clearing for payout {payout_id}",
                ),
            ],
        )

        hold_journal = self._ledger.post_journal(
            session=session,
            tenant_id=tenant_id,
            request=hold_req,
            created_by_user_id=caller_user.id,
        )

        payout = PayoutRequest(
            id=payout_id,
            tenant_id=tenant_id,
            agent_membership_id=caller_membership.id,
            agent_profile_id=prof.id,
            account_id=acct.id,
            amount=request.amount,
            currency=acct.currency,
            status=PayoutStatus.PENDING_REVIEW.value,
            payout_hold_journal_id=hold_journal.id,
            idempotency_key=clean_key,
            bank_account_snapshot=request.bank_account_details,
            request_note=request.request_note,
            created_by_user_id=caller_user.id,
            created_at=now,
            updated_at=now,
        )
        session.add(payout)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="payout.requested",
                actor_category="tenant_member",
                actor_ref=str(caller_user.id),
                action="payout.requested",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="payout_request",
                target_entity_ref=str(payout.id),
            ),
        )

        return payout

    def review_payout(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        payout_id: UUID,
        reviewer_membership: TenantMembership,
        reviewer_user: User,
        request: PayoutReviewRequest,
    ) -> PayoutRequest:
        """Community Admin reviews payout request. Self-approval is strictly rejected."""
        payout = session.execute(
            select(PayoutRequest)
            .where(
                PayoutRequest.id == payout_id,
                PayoutRequest.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if payout is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PAYOUT_NOT_FOUND", "message": "Payout request not found in this community."},
            )

        # Anti-Self-Approval Safeguard: Community Admin cannot approve or review their own request
        if (
            payout.agent_membership_id == reviewer_membership.id
            or payout.created_by_user_id == reviewer_user.id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "SELF_APPROVAL_PROHIBITED",
                    "message": "Community Admins are strictly prohibited from reviewing or approving their own payout requests.",
                },
            )

        if payout.status in TERMINAL_PAYOUT_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "TERMINAL_STATE",
                    "message": f"Payout request is in terminal state '{payout.status}' and cannot be reviewed.",
                },
            )

        if payout.status != PayoutStatus.PENDING_REVIEW.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "INVALID_STATE",
                    "message": f"Payout request is in status '{payout.status}'. Only PENDING_REVIEW requests can be reviewed.",
                },
            )

        now = datetime.now(UTC)

        if request.decision == "APPROVE":
            payout.status = PayoutStatus.APPROVED.value
            payout.reviewed_by_membership_id = reviewer_membership.id
            payout.reviewed_at = now
            payout.review_notes = request.notes or request.reason
            payout.updated_at = now

            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="payout.approved",
                    actor_category="tenant_member",
                    actor_ref=str(reviewer_user.id),
                    action="payout.approved",
                    outcome="success",
                    tenant_id=tenant_id,
                    target_entity_type="payout_request",
                    target_entity_ref=str(payout.id),
                ),
            )
            return payout

        elif request.decision == "REJECT":
            # Reversal: release escrow hold back to agent account
            clearing_acct = self._ledger.get_or_create_system_account(
                session,
                tenant_id=tenant_id,
                account_type=AccountType.PAYOUT_CLEARING,
                currency=payout.currency,
            )

            reversal_req = JournalCreateRequest(
                idempotency_key=f"PAYOUT-REVERSAL-{payout.id}",
                description=f"Reversal of rejected payout {payout.id}: {request.reason}",
                reference_type=JournalReferenceType.REVERSAL.value,
                reference_id=str(payout.id),
                currency=payout.currency,
                entries=[
                    JournalEntryLineSpec(
                        account_id=clearing_acct.id,
                        entry_type=EntryType.DEBIT.value,
                        amount=payout.amount,
                        description=f"Release escrow for rejected payout {payout.id}",
                    ),
                    JournalEntryLineSpec(
                        account_id=payout.account_id,
                        entry_type=EntryType.CREDIT.value,
                        amount=payout.amount,
                        description=f"Restoration of commission for rejected payout {payout.id}",
                    ),
                ],
            )

            rev_journal = self._ledger.post_journal(
                session=session,
                tenant_id=tenant_id,
                request=reversal_req,
                created_by_user_id=reviewer_user.id,
            )

            payout.status = PayoutStatus.REJECTED.value
            payout.reviewed_by_membership_id = reviewer_membership.id
            payout.reviewed_at = now
            payout.rejection_reason = request.reason
            payout.review_notes = request.notes
            payout.reversal_journal_id = rev_journal.id
            payout.updated_at = now

            self._audit.record_mandatory(
                session,
                AuditEventDraft(
                    event_type="payout.rejected",
                    actor_category="tenant_member",
                    actor_ref=str(reviewer_user.id),
                    action="payout.rejected",
                    outcome="success",
                    tenant_id=tenant_id,
                    target_entity_type="payout_request",
                    target_entity_ref=str(payout.id),
                ),
            )
            return payout
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "INVALID_DECISION", "message": f"Unsupported review decision '{request.decision}'."},
            )

    def disburse_payout(
        self,
        session: Session,
        *,
        payout_id: UUID,
        operator: PlatformOperator,
        request: PayoutDisburseRequest,
    ) -> PayoutRequest:
        """Platform Super Admin authorizes and confirms disbursement with verifiable reference evidence."""
        payout = session.execute(
            select(PayoutRequest)
            .where(PayoutRequest.id == payout_id)
            .with_for_update()
        ).scalar_one_or_none()

        if payout is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PAYOUT_NOT_FOUND", "message": "Payout request not found."},
            )

        if payout.status == PayoutStatus.PENDING_REVIEW.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "NOT_APPROVED_BY_COMMUNITY",
                    "message": "Payout request must be approved by Community Admin before Platform Super Admin disbursement.",
                },
            )

        if payout.status in TERMINAL_PAYOUT_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "TERMINAL_STATE",
                    "message": f"Payout request is in terminal state '{payout.status}' and cannot be disbursed.",
                },
            )

        clean_ref = request.disbursement_reference.strip()
        if len(clean_ref) < 3:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "INVALID_DISBURSEMENT_REFERENCE",
                    "message": "Verifiable disbursement reference / UTR is strictly mandatory.",
                },
            )

        now = datetime.now(UTC)

        # Post PAYOUT_DISBURSEMENT double-entry journal:
        # Debit: PAYOUT_CLEARING (clears escrow hold)
        # Credit: COMMUNITY_CLEARING (settles banking outflow)
        clearing_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=payout.tenant_id,
            account_type=AccountType.PAYOUT_CLEARING,
            currency=payout.currency,
        )
        settlement_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=payout.tenant_id,
            account_type=AccountType.COMMUNITY_CLEARING,
            currency=payout.currency,
        )

        disburse_req = JournalCreateRequest(
            idempotency_key=f"PAYOUT-DISBURSE-{payout.id}",
            description=f"Disbursement settlement for payout {payout.id} (ref: {clean_ref})",
            reference_type=JournalReferenceType.PAYOUT_DISBURSEMENT.value,
            reference_id=str(payout.id),
            currency=payout.currency,
            entries=[
                JournalEntryLineSpec(
                    account_id=clearing_acct.id,
                    entry_type=EntryType.DEBIT.value,
                    amount=payout.amount,
                    description=f"Clear escrow for disbursed payout {payout.id}",
                ),
                JournalEntryLineSpec(
                    account_id=settlement_acct.id,
                    entry_type=EntryType.CREDIT.value,
                    amount=payout.amount,
                    description=f"Disbursement transfer {clean_ref}",
                ),
            ],
        )

        disb_journal = self._ledger.post_journal(
            session=session,
            tenant_id=payout.tenant_id,
            request=disburse_req,
            created_by_user_id=operator.user_id,
        )

        payout.status = PayoutStatus.PAID.value
        payout.disbursed_by_user_id = operator.user_id
        payout.disbursed_at = now
        payout.disbursement_reference = clean_ref
        payout.disbursement_method = request.disbursement_method.strip()
        payout.disbursement_journal_id = disb_journal.id
        payout.updated_at = now

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="payout.disbursed",
                actor_category="platform_operator",
                actor_ref=str(operator.user_id),
                action="payout.disbursed",
                outcome="success",
                tenant_id=payout.tenant_id,
                target_entity_type="payout_request",
                target_entity_ref=str(payout.id),
            ),
        )

        return payout

    def fail_disbursement(
        self,
        session: Session,
        *,
        payout_id: UUID,
        operator: PlatformOperator,
        request: PayoutFailRequest,
    ) -> PayoutRequest:
        """Platform Super Admin marks disbursement failed and returns held funds to agent account."""
        payout = session.execute(
            select(PayoutRequest)
            .where(PayoutRequest.id == payout_id)
            .with_for_update()
        ).scalar_one_or_none()

        if payout is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PAYOUT_NOT_FOUND", "message": "Payout request not found."},
            )

        if payout.status in TERMINAL_PAYOUT_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "TERMINAL_STATE",
                    "message": f"Payout request is in terminal state '{payout.status}'.",
                },
            )

        clean_reason = request.reason.strip()
        now = datetime.now(UTC)

        # Reversal journal: Debit PAYOUT_CLEARING, Credit AGENT_COMMISSION
        clearing_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=payout.tenant_id,
            account_type=AccountType.PAYOUT_CLEARING,
            currency=payout.currency,
        )

        reversal_req = JournalCreateRequest(
            idempotency_key=f"PAYOUT-FAIL-REV-{payout.id}",
            description=f"Disbursement failure reversal for payout {payout.id}: {clean_reason}",
            reference_type=JournalReferenceType.REVERSAL.value,
            reference_id=str(payout.id),
            currency=payout.currency,
            entries=[
                JournalEntryLineSpec(
                    account_id=clearing_acct.id,
                    entry_type=EntryType.DEBIT.value,
                    amount=payout.amount,
                    description=f"Release escrow for failed payout {payout.id}",
                ),
                JournalEntryLineSpec(
                    account_id=payout.account_id,
                    entry_type=EntryType.CREDIT.value,
                    amount=payout.amount,
                    description=f"Refund to agent for failed payout {payout.id}",
                ),
            ],
        )

        rev_journal = self._ledger.post_journal(
            session=session,
            tenant_id=payout.tenant_id,
            request=reversal_req,
            created_by_user_id=operator.user_id,
        )

        payout.status = PayoutStatus.FAILED.value
        payout.failure_reason = clean_reason
        payout.reversal_journal_id = rev_journal.id
        payout.updated_at = now

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="payout.failed",
                actor_category="platform_operator",
                actor_ref=str(operator.user_id),
                action="payout.failed",
                outcome="success",
                tenant_id=payout.tenant_id,
                target_entity_type="payout_request",
                target_entity_ref=str(payout.id),
            ),
        )

        return payout

    def cancel_payout(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        payout_id: UUID,
        caller_membership: TenantMembership,
        caller_user: User,
        request: PayoutCancelRequest,
    ) -> PayoutRequest:
        """Agent cancels their own pending payout request before review."""
        payout = session.execute(
            select(PayoutRequest)
            .where(
                PayoutRequest.id == payout_id,
                PayoutRequest.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if payout is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PAYOUT_NOT_FOUND", "message": "Payout request not found in this community."},
            )

        if payout.agent_membership_id != caller_membership.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "NOT_OWNER", "message": "You can only cancel your own payout requests."},
            )

        if payout.status != PayoutStatus.PENDING_REVIEW.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "CANNOT_CANCEL",
                    "message": f"Only requests in PENDING_REVIEW can be cancelled. Current status is '{payout.status}'.",
                },
            )

        now = datetime.now(UTC)
        reason = request.reason or "Cancelled by agent"

        clearing_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=tenant_id,
            account_type=AccountType.PAYOUT_CLEARING,
            currency=payout.currency,
        )

        reversal_req = JournalCreateRequest(
            idempotency_key=f"PAYOUT-CANCEL-REV-{payout.id}",
            description=f"Agent cancellation reversal for payout {payout.id}",
            reference_type=JournalReferenceType.REVERSAL.value,
            reference_id=str(payout.id),
            currency=payout.currency,
            entries=[
                JournalEntryLineSpec(
                    account_id=clearing_acct.id,
                    entry_type=EntryType.DEBIT.value,
                    amount=payout.amount,
                    description=f"Release escrow for cancelled payout {payout.id}",
                ),
                JournalEntryLineSpec(
                    account_id=payout.account_id,
                    entry_type=EntryType.CREDIT.value,
                    amount=payout.amount,
                    description=f"Refund to agent for cancelled payout {payout.id}",
                ),
            ],
        )

        rev_journal = self._ledger.post_journal(
            session=session,
            tenant_id=tenant_id,
            request=reversal_req,
            created_by_user_id=caller_user.id,
        )

        payout.status = PayoutStatus.CANCELLED.value
        payout.rejection_reason = reason
        payout.reversal_journal_id = rev_journal.id
        payout.updated_at = now

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="payout.cancelled",
                actor_category="tenant_member",
                actor_ref=str(caller_user.id),
                action="payout.cancelled",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="payout_request",
                target_entity_ref=str(payout.id),
            ),
        )

        return payout

    def list_payouts(
        self,
        session: Session,
        *,
        tenant_id: UUID | None = None,
        agent_membership_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PayoutRequest], int]:
        """Lists and counts payout requests with tenant, agent, and status filters."""
        query = select(PayoutRequest)
        count_query = select(func.count(PayoutRequest.id))

        if tenant_id is not None:
            query = query.where(PayoutRequest.tenant_id == tenant_id)
            count_query = count_query.where(PayoutRequest.tenant_id == tenant_id)

        if agent_membership_id is not None:
            query = query.where(PayoutRequest.agent_membership_id == agent_membership_id)
            count_query = count_query.where(PayoutRequest.agent_membership_id == agent_membership_id)

        if status_filter is not None:
            query = query.where(PayoutRequest.status == status_filter)
            count_query = count_query.where(PayoutRequest.status == status_filter)

        total = session.execute(count_query).scalar() or 0
        items = list(
            session.execute(
                query.order_by(PayoutRequest.created_at.desc()).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )
        return items, total

    def get_payout_by_id(
        self,
        session: Session,
        *,
        payout_id: UUID,
        tenant_id: UUID | None = None,
    ) -> PayoutRequest:
        """Retrieves a single payout request by ID with optional tenant scope."""
        query = select(PayoutRequest).where(PayoutRequest.id == payout_id)
        if tenant_id is not None:
            query = query.where(PayoutRequest.tenant_id == tenant_id)

        payout = session.execute(query).scalar_one_or_none()
        if payout is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "PAYOUT_NOT_FOUND", "message": "Payout request not found."},
            )
        return payout


_global_payout_service: PayoutService | None = None


def get_payout_service() -> PayoutService:
    global _global_payout_service
    if _global_payout_service is None:
        _global_payout_service = PayoutService()
    return _global_payout_service
