from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
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
    CommissionLedgerEntry,
    EntryType,
    JournalReferenceType,
    JournalStatus,
)
from app.modules.commissions.schemas import (
    CommissionAccountSummary,
    JournalCreateRequest,
    JournalDetailResponse,
    JournalEntryLineSpec,
    JournalSummary,
    LedgerEntriesListResponse,
    LedgerEntrySummary,
)
from app.modules.identity.models import TenantMembership
from app.modules.payments.models import PaymentTransaction
from app.modules.relationships.models import MemberRelationship, RelationshipStatus, RelationshipType
from app.modules.subscriptions.models import MemberSubscription
from app.modules.tenants.models import Tenant


class FinancialLedgerService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self._audit = audit_service or AuditService()

    def get_or_create_system_account(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        account_type: AccountType,
        currency: str = "INR",
    ) -> CommissionAccount:
        curr = currency.upper().strip()
        acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.account_type == account_type.value,
                CommissionAccount.currency == curr,
                CommissionAccount.agent_membership_id.is_(None),
            )
        ).scalar_one_or_none()

        if acct is not None:
            return acct

        now = datetime.now(UTC)
        acct = CommissionAccount(
            id=uuid4(),
            tenant_id=tenant_id,
            agent_membership_id=None,
            agent_profile_id=None,
            account_type=account_type.value,
            account_number=f"SYS-{tenant_id.hex[:8].upper()}-{account_type.value}",
            currency=curr,
            current_balance=Decimal("0.00"),
            status=AccountStatus.ACTIVE.value,
            allow_negative=True,
            created_at=now,
            updated_at=now,
        )
        session.add(acct)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="financial.account.created",
                actor_category="system",
                actor_ref="system:ledger_service",
                action="financial.account.created",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="commission_account",
                target_entity_ref=str(acct.id),
            ),
        )
        return acct

    def get_or_create_agent_account(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        agent_membership_id: UUID,
        agent_profile_id: UUID,
        currency: str = "INR",
    ) -> CommissionAccount:
        curr = currency.upper().strip()
        acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.tenant_id == tenant_id,
                CommissionAccount.agent_membership_id == agent_membership_id,
                CommissionAccount.currency == curr,
            )
        ).scalar_one_or_none()

        if acct is not None:
            return acct

        profile = session.execute(
            select(AgentProfile).where(
                AgentProfile.id == agent_profile_id,
                AgentProfile.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "AGENT_NOT_FOUND", "message": "Agent profile not found in this community."},
            )

        now = datetime.now(UTC)
        acct = CommissionAccount(
            id=uuid4(),
            tenant_id=tenant_id,
            agent_membership_id=agent_membership_id,
            agent_profile_id=agent_profile_id,
            account_type=AccountType.AGENT_COMMISSION.value,
            account_number=f"AGT-{profile.ref_id}-COMM",
            currency=curr,
            current_balance=Decimal("0.00"),
            status=AccountStatus.ACTIVE.value,
            allow_negative=False,
            created_at=now,
            updated_at=now,
        )
        session.add(acct)
        session.flush()

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="financial.account.created",
                actor_category="tenant_member",
                actor_ref=str(profile.user_id),
                action="financial.account.created",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="commission_account",
                target_entity_ref=str(acct.id),
            ),
        )
        return acct

    def post_journal(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        request: JournalCreateRequest,
        created_by_user_id: UUID | None,
    ) -> CommissionJournal:
        curr = request.currency.upper().strip()
        if len(curr) != 3:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "INVALID_CURRENCY", "message": "Currency must be an ISO-4217 3-letter code."},
            )

        if len(request.entries) < 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "INVALID_JOURNAL", "message": "A double-entry journal must contain at least 2 entries."},
            )

        # 1. Exact Precision & Double-Entry Equality Check
        sum_debit = Decimal("0.00")
        sum_credit = Decimal("0.00")

        for entry in request.entries:
            amt = entry.amount
            if amt <= Decimal("0.00"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_AMOUNT", "message": f"Ledger entry amount ({amt}) must be strictly positive."},
                )
            if entry.entry_type == EntryType.DEBIT.value:
                sum_debit += amt
            elif entry.entry_type == EntryType.CREDIT.value:
                sum_credit += amt
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_ENTRY_TYPE", "message": f"Invalid entry type '{entry.entry_type}'."},
                )

        if sum_debit != sum_credit:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "UNBALANCED_JOURNAL",
                    "message": f"Double-entry journal is unbalanced. Total debits ({sum_debit}) != total credits ({sum_credit}).",
                },
            )

        total_journal_amount = sum_debit

        # 2. Idempotency Check
        clean_key = request.idempotency_key.strip()
        existing = session.execute(
            select(CommissionJournal).where(
                CommissionJournal.tenant_id == tenant_id,
                CommissionJournal.idempotency_key == clean_key,
            )
        ).scalar_one_or_none()

        if existing is not None:
            if (
                existing.total_amount == total_journal_amount
                and existing.currency == curr
                and existing.description == request.description.strip()
            ):
                # Idempotent replay: return existing journal safely
                return existing
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "IDEMPOTENCY_CONFLICT",
                    "message": "Idempotency key previously used with different journal payload.",
                },
            )

        # 3. Deterministic Lock Acquisition (Row-Level Locking on Affected Accounts)
        account_ids = sorted(list(set(e.account_id for e in request.entries)))
        accounts_map: dict[UUID, CommissionAccount] = {}

        for aid in account_ids:
            acct = session.execute(
                select(CommissionAccount)
                .where(CommissionAccount.id == aid)
                .with_for_update()
            ).scalar_one_or_none()

            if acct is None or acct.tenant_id != tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": "ACCOUNT_NOT_FOUND",
                        "message": f"Account {aid} does not exist in this community.",
                    },
                )
            if acct.currency != curr:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "CURRENCY_MISMATCH",
                        "message": f"Account {acct.account_number} currency ({acct.currency}) does not match journal currency ({curr}).",
                    },
                )
            if acct.status != AccountStatus.ACTIVE.value:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": "ACCOUNT_NOT_ACTIVE",
                        "message": f"Account {acct.account_number} is {acct.status} and cannot accept postings.",
                    },
                )
            accounts_map[aid] = acct

        # 4. Preliminary Balance and Overdraft Verification
        running_balances = {aid: accounts_map[aid].current_balance for aid in account_ids}

        for entry in request.entries:
            acct = accounts_map[entry.account_id]
            current = running_balances[entry.account_id]

            if acct.account_type == AccountType.AGENT_COMMISSION.value:
                # Agent liability/asset: CREDIT increases balance, DEBIT decreases balance
                if entry.entry_type == EntryType.CREDIT.value:
                    running_balances[entry.account_id] = current + entry.amount
                else:
                    new_val = current - entry.amount
                    if not acct.allow_negative and new_val < Decimal("0.00"):
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={
                                "error": "INSUFFICIENT_FUNDS",
                                "message": f"Account {acct.account_number} has balance {current}, insufficient for debit of {entry.amount}.",
                            },
                        )
                    running_balances[entry.account_id] = new_val
            else:
                # System clearing/expense: DEBIT increases, CREDIT decreases
                if entry.entry_type == EntryType.DEBIT.value:
                    running_balances[entry.account_id] = current + entry.amount
                else:
                    running_balances[entry.account_id] = current - entry.amount

        # 5. Atomic Persistence
        now = datetime.now(UTC)
        journal_id = uuid4()
        journal_num = f"JRN-{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6].upper()}"

        journal = CommissionJournal(
            id=journal_id,
            tenant_id=tenant_id,
            journal_number=journal_num,
            description=request.description.strip(),
            reference_type=request.reference_type.strip(),
            reference_id=request.reference_id.strip() if request.reference_id else None,
            idempotency_key=clean_key,
            currency=curr,
            total_amount=total_journal_amount,
            status=JournalStatus.POSTED.value,
            posted_at=now,
            created_by_user_id=created_by_user_id,
            created_at=now,
        )
        session.add(journal)

        # Track intermediate balances for each ledger line item
        current_step_balances = {aid: accounts_map[aid].current_balance for aid in account_ids}

        for entry in request.entries:
            acct = accounts_map[entry.account_id]
            curr_val = current_step_balances[entry.account_id]

            if acct.account_type == AccountType.AGENT_COMMISSION.value:
                if entry.entry_type == EntryType.CREDIT.value:
                    next_val = curr_val + entry.amount
                else:
                    next_val = curr_val - entry.amount
            else:
                if entry.entry_type == EntryType.DEBIT.value:
                    next_val = curr_val + entry.amount
                else:
                    next_val = curr_val - entry.amount

            current_step_balances[entry.account_id] = next_val

            ledger_entry = CommissionLedgerEntry(
                id=uuid4(),
                tenant_id=tenant_id,
                journal_id=journal_id,
                account_id=entry.account_id,
                entry_type=entry.entry_type,
                amount=entry.amount,
                currency=curr,
                balance_after=next_val,
                description=entry.description.strip() if entry.description else None,
                created_at=now,
            )
            session.add(ledger_entry)

        # Apply final balances to accounts
        for aid, final_bal in current_step_balances.items():
            accounts_map[aid].current_balance = final_bal
            accounts_map[aid].updated_at = now

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="financial.journal.posted",
                actor_category="system" if created_by_user_id is None else "tenant_member",
                actor_ref="system:ledger_service" if created_by_user_id is None else str(created_by_user_id),
                action="financial.journal.posted",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="commission_journal",
                target_entity_ref=str(journal_id),
            ),
        )

        return journal

    def reverse_journal(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        original_journal_id: UUID,
        reason: str,
        created_by_user_id: UUID | None,
        idempotency_key: str,
    ) -> CommissionJournal:
        orig = session.execute(
            select(CommissionJournal)
            .where(
                CommissionJournal.id == original_journal_id,
                CommissionJournal.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if orig is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "JOURNAL_NOT_FOUND", "message": "Original journal not found in this community."},
            )

        if orig.status == JournalStatus.REVERSED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "JOURNAL_ALREADY_REVERSED", "message": "Original journal is already reversed."},
            )

        orig_entries = session.execute(
            select(CommissionLedgerEntry)
            .where(CommissionLedgerEntry.journal_id == original_journal_id)
            .order_by(CommissionLedgerEntry.created_at.asc())
        ).scalars().all()

        if not orig_entries:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "EMPTY_ORIGINAL_JOURNAL", "message": "Original journal has no entries to reverse."},
            )

        # Invert entries: DEBIT becomes CREDIT, CREDIT becomes DEBIT
        inverted_lines: list[JournalEntryLineSpec] = []
        for entry in orig_entries:
            inverted_type = EntryType.CREDIT.value if entry.entry_type == EntryType.DEBIT.value else EntryType.DEBIT.value
            inverted_lines.append(
                JournalEntryLineSpec(
                    account_id=entry.account_id,
                    entry_type=inverted_type,
                    amount=entry.amount,
                    description=f"Reversal of {orig.journal_number}: {reason.strip()}",
                )
            )

        reversal_req = JournalCreateRequest(
            idempotency_key=idempotency_key,
            description=f"Compensating Reversal of {orig.journal_number}",
            reference_type=JournalReferenceType.REVERSAL.value,
            reference_id=str(orig.id),
            currency=orig.currency,
            entries=inverted_lines,
        )

        reversal_journal = self.post_journal(
            session,
            tenant_id=tenant_id,
            request=reversal_req,
            created_by_user_id=created_by_user_id,
        )

        orig.status = JournalStatus.REVERSED.value

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="financial.journal.reversed",
                actor_category="system" if created_by_user_id is None else "tenant_member",
                actor_ref="system:ledger_service" if created_by_user_id is None else str(created_by_user_id),
                action="financial.journal.reversed",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="commission_journal",
                target_entity_ref=str(orig.id),
            ),
        )

        return reversal_journal

    def get_account_by_id(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        account_id: UUID,
    ) -> CommissionAccount:
        acct = session.execute(
            select(CommissionAccount).where(
                CommissionAccount.id == account_id,
                CommissionAccount.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if acct is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "ACCOUNT_NOT_FOUND", "message": "Commission account not found in this community."},
            )
        return acct

    def get_agent_account_by_user_id(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        user_id: UUID,
    ) -> CommissionAccount:
        profile = session.execute(
            select(AgentProfile).where(
                AgentProfile.user_id == user_id,
                AgentProfile.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "AGENT_PROFILE_NOT_FOUND", "message": "Agent profile not found."},
            )

        return self.get_or_create_agent_account(
            session,
            tenant_id=tenant_id,
            agent_membership_id=profile.membership_id,
            agent_profile_id=profile.id,
        )

    def list_account_entries(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        account_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CommissionLedgerEntry], int]:
        # Validate account ownership
        self.get_account_by_id(session, tenant_id=tenant_id, account_id=account_id)

        count_stmt = (
            select(func.count(CommissionLedgerEntry.id))
            .where(
                CommissionLedgerEntry.account_id == account_id,
                CommissionLedgerEntry.tenant_id == tenant_id,
            )
        )
        total = session.execute(count_stmt).scalar() or 0

        entries_stmt = (
            select(CommissionLedgerEntry)
            .where(
                CommissionLedgerEntry.account_id == account_id,
                CommissionLedgerEntry.tenant_id == tenant_id,
            )
            .order_by(CommissionLedgerEntry.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list(session.execute(entries_stmt).scalars().all())
        return items, total

    def get_journal_detail(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        journal_id: UUID,
    ) -> tuple[CommissionJournal, list[CommissionLedgerEntry]]:
        journal = session.execute(
            select(CommissionJournal).where(
                CommissionJournal.id == journal_id,
                CommissionJournal.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if journal is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "JOURNAL_NOT_FOUND", "message": "Commission journal not found in this community."},
            )

        entries = session.execute(
            select(CommissionLedgerEntry)
            .where(
                CommissionLedgerEntry.journal_id == journal_id,
                CommissionLedgerEntry.tenant_id == tenant_id,
            )
            .order_by(CommissionLedgerEntry.created_at.asc())
        ).scalars().all()

        return journal, list(entries)


_global_ledger_service = FinancialLedgerService()


def get_financial_ledger_service() -> FinancialLedgerService:
    return _global_ledger_service


@dataclass(frozen=True)
class TierCommissionAttribution:
    tier: int
    agent_membership_id: UUID
    agent_profile_id: UUID
    rate_percent: Decimal
    commission_amount: Decimal
    account_id: UUID


@dataclass(frozen=True)
class CommissionAttributionResult:
    subscription_id: UUID
    agreed_amount: Decimal
    attributions: list[TierCommissionAttribution]
    total_commission_distributed: Decimal
    journal: CommissionJournal | None


class CommissionAttributionService:
    """Calculates and attributes multi-tier referral commissions to the financial ledger."""

    def __init__(
        self,
        ledger_service: FinancialLedgerService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._audit = audit_service or AuditService()
        self._ledger = ledger_service or FinancialLedgerService(audit_service=self._audit)

    def attribute_subscription_commissions(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        subscription: MemberSubscription,
        payment_transaction: PaymentTransaction,
        created_by_user_id: UUID | None = None,
    ) -> CommissionAttributionResult:
        """
        Attributes referral commissions across up to 3 tiers along the REFERRED_BY hierarchy:
        - Tier 1 (Direct Agent): effective commission rate (override or tenant default, e.g. 10%)
        - Tier 2 (Parent Agent): fixed 3%
        - Tier 3 (Grandparent Agent): fixed 1%

        Rules:
        - Missing or inactive ancestors: unearned commission is retained by tenant (no roll-up).
        - Commissionable base: gross agreed_amount without deductions.
        - Idempotent and double-entry balanced: debits COMMUNITY_EXPENSE, credits AGENT_COMMISSION.
        """
        gross_amount = subscription.agreed_amount
        if gross_amount <= Decimal("0.00"):
            return CommissionAttributionResult(
                subscription_id=subscription.id,
                agreed_amount=gross_amount,
                attributions=[],
                total_commission_distributed=Decimal("0.00"),
                journal=None,
            )

        # 1. Fetch tenant default commission rate
        tenant = session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()
        tenant_default_rate = (
            Decimal(str(tenant.default_commission_rate))
            if (tenant and tenant.default_commission_rate is not None)
            else Decimal("10.00")
        )

        attributions: list[TierCommissionAttribution] = []
        visited_members: set[UUID] = {subscription.subscriber_membership_id}

        # 2. Lookup Tier 1 Referrer (Direct Agent of Subscriber)
        rel1 = session.execute(
            select(MemberRelationship).where(
                MemberRelationship.tenant_id == tenant_id,
                MemberRelationship.child_member_id == subscription.subscriber_membership_id,
                MemberRelationship.relationship_type == RelationshipType.REFERRED_BY.value,
                MemberRelationship.status == RelationshipStatus.ACTIVE.value,
            )
        ).scalar_one_or_none()

        tier1_member_id: UUID | None = None
        if rel1 is not None and rel1.parent_member_id not in visited_members:
            tier1_member_id = rel1.parent_member_id
            visited_members.add(tier1_member_id)

            # Check if active agent membership and profile
            tm1 = session.execute(
                select(TenantMembership).where(
                    TenantMembership.id == tier1_member_id,
                    TenantMembership.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()

            prof1 = session.execute(
                select(AgentProfile).where(
                    AgentProfile.membership_id == tier1_member_id,
                    AgentProfile.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()

            if tm1 is not None and tm1.status == "active" and prof1 is not None:
                tier1_rate = (
                    Decimal(str(prof1.commission_rate_override))
                    if prof1.commission_rate_override is not None
                    else tenant_default_rate
                )
                tier1_amount = (gross_amount * tier1_rate / Decimal("100")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if tier1_amount > Decimal("0.00"):
                    acct1 = self._ledger.get_or_create_agent_account(
                        session,
                        tenant_id=tenant_id,
                        agent_membership_id=tier1_member_id,
                        agent_profile_id=prof1.id,
                        currency=subscription.currency,
                    )
                    attributions.append(
                        TierCommissionAttribution(
                            tier=1,
                            agent_membership_id=tier1_member_id,
                            agent_profile_id=prof1.id,
                            rate_percent=tier1_rate,
                            commission_amount=tier1_amount,
                            account_id=acct1.id,
                        )
                    )

        # 3. Lookup Tier 2 Referrer (Parent Agent of Tier 1)
        tier2_member_id: UUID | None = None
        if tier1_member_id is not None:
            rel2 = session.execute(
                select(MemberRelationship).where(
                    MemberRelationship.tenant_id == tenant_id,
                    MemberRelationship.child_member_id == tier1_member_id,
                    MemberRelationship.relationship_type == RelationshipType.REFERRED_BY.value,
                    MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()

            if rel2 is not None and rel2.parent_member_id not in visited_members:
                tier2_member_id = rel2.parent_member_id
                visited_members.add(tier2_member_id)

                tm2 = session.execute(
                    select(TenantMembership).where(
                        TenantMembership.id == tier2_member_id,
                        TenantMembership.tenant_id == tenant_id,
                    )
                ).scalar_one_or_none()

                prof2 = session.execute(
                    select(AgentProfile).where(
                        AgentProfile.membership_id == tier2_member_id,
                        AgentProfile.tenant_id == tenant_id,
                    )
                ).scalar_one_or_none()

                if tm2 is not None and tm2.status == "active" and prof2 is not None:
                    tier2_rate = Decimal("3.00")
                    tier2_amount = (gross_amount * tier2_rate / Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    if tier2_amount > Decimal("0.00"):
                        acct2 = self._ledger.get_or_create_agent_account(
                            session,
                            tenant_id=tenant_id,
                            agent_membership_id=tier2_member_id,
                            agent_profile_id=prof2.id,
                            currency=subscription.currency,
                        )
                        attributions.append(
                            TierCommissionAttribution(
                                tier=2,
                                agent_membership_id=tier2_member_id,
                                agent_profile_id=prof2.id,
                                rate_percent=tier2_rate,
                                commission_amount=tier2_amount,
                                account_id=acct2.id,
                            )
                        )

        # 4. Lookup Tier 3 Referrer (Grandparent Agent of Tier 1)
        if tier2_member_id is not None:
            rel3 = session.execute(
                select(MemberRelationship).where(
                    MemberRelationship.tenant_id == tenant_id,
                    MemberRelationship.child_member_id == tier2_member_id,
                    MemberRelationship.relationship_type == RelationshipType.REFERRED_BY.value,
                    MemberRelationship.status == RelationshipStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()

            if rel3 is not None and rel3.parent_member_id not in visited_members:
                tier3_member_id = rel3.parent_member_id
                visited_members.add(tier3_member_id)

                tm3 = session.execute(
                    select(TenantMembership).where(
                        TenantMembership.id == tier3_member_id,
                        TenantMembership.tenant_id == tenant_id,
                    )
                ).scalar_one_or_none()

                prof3 = session.execute(
                    select(AgentProfile).where(
                        AgentProfile.membership_id == tier3_member_id,
                        AgentProfile.tenant_id == tenant_id,
                    )
                ).scalar_one_or_none()

                if tm3 is not None and tm3.status == "active" and prof3 is not None:
                    tier3_rate = Decimal("1.00")
                    tier3_amount = (gross_amount * tier3_rate / Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    if tier3_amount > Decimal("0.00"):
                        acct3 = self._ledger.get_or_create_agent_account(
                            session,
                            tenant_id=tenant_id,
                            agent_membership_id=tier3_member_id,
                            agent_profile_id=prof3.id,
                            currency=subscription.currency,
                        )
                        attributions.append(
                            TierCommissionAttribution(
                                tier=3,
                                agent_membership_id=tier3_member_id,
                                agent_profile_id=prof3.id,
                                rate_percent=tier3_rate,
                                commission_amount=tier3_amount,
                                account_id=acct3.id,
                            )
                        )

        # 5. If no active agent was eligible, unearned commission is retained by tenant
        if not attributions:
            return CommissionAttributionResult(
                subscription_id=subscription.id,
                agreed_amount=gross_amount,
                attributions=[],
                total_commission_distributed=Decimal("0.00"),
                journal=None,
            )

        # 6. Post double-entry journal to financial ledger
        total_commission = sum((attr.commission_amount for attr in attributions), Decimal("0.00"))
        expense_acct = self._ledger.get_or_create_system_account(
            session,
            tenant_id=tenant_id,
            account_type=AccountType.COMMUNITY_EXPENSE,
            currency=subscription.currency,
        )

        entries = [
            JournalEntryLineSpec(
                account_id=expense_acct.id,
                entry_type=EntryType.DEBIT.value,
                amount=total_commission,
                description=f"Referral commission expense for subscription {subscription.id}",
            )
        ]

        for attr in attributions:
            entries.append(
                JournalEntryLineSpec(
                    account_id=attr.account_id,
                    entry_type=EntryType.CREDIT.value,
                    amount=attr.commission_amount,
                    description=f"Tier {attr.tier} commission ({attr.rate_percent}%) on subscription {subscription.id}",
                )
            )

        idempotency_key = f"COMM-ATTRIB-{subscription.id}-{payment_transaction.id}"
        journal_req = JournalCreateRequest(
            idempotency_key=idempotency_key,
            description=f"Referral commissions for subscription {subscription.id} ({payment_transaction.gateway_payment_id})",
            reference_type=JournalReferenceType.REFERRAL_COMMISSION.value,
            reference_id=str(subscription.id),
            currency=subscription.currency,
            entries=entries,
        )

        journal = self._ledger.post_journal(
            session=session,
            tenant_id=tenant_id,
            request=journal_req,
            created_by_user_id=created_by_user_id,
        )

        self._audit.record_mandatory(
            session,
            AuditEventDraft(
                event_type="subscription.commissions.attributed",
                actor_category="system",
                actor_ref="system:commission_attribution_service",
                action="commission.attributed",
                outcome="success",
                tenant_id=tenant_id,
                target_entity_type="member_subscription",
                target_entity_ref=str(subscription.id),
            ),
        )

        return CommissionAttributionResult(
            subscription_id=subscription.id,
            agreed_amount=gross_amount,
            attributions=attributions,
            total_commission_distributed=total_commission,
            journal=journal,
        )


_global_commission_attribution_service = CommissionAttributionService()


def get_commission_attribution_service() -> CommissionAttributionService:
    return _global_commission_attribution_service
