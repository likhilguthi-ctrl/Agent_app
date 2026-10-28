from __future__ import annotations

from typing import Any, Protocol


class DNSLookupFailure(Exception):
    """A bounded DNS query could not return a trustworthy TXT answer."""


class TXTRecordResolver(Protocol):
    async def lookup_txt(
        self,
        owner_name: str,
        *,
        timeout_seconds: float,
        max_records: int,
        max_response_bytes: int,
    ) -> tuple[str, ...]: ...


class SystemDNSResolver:
    """Resolve only the caller-independent DNS TXT owner supplied by the service."""

    def __init__(self, resolver: Any | None = None) -> None:
        self._resolver = resolver

    async def lookup_txt(
        self,
        owner_name: str,
        *,
        timeout_seconds: float,
        max_records: int,
        max_response_bytes: int,
    ) -> tuple[str, ...]:
        resolver = self._resolver
        if resolver is None:
            try:
                import dns.asyncresolver
            except ImportError as exc:
                raise DNSLookupFailure from exc
            resolver = dns.asyncresolver.Resolver(configure=True)
            self._resolver = resolver
        try:
            answer = await resolver.resolve(
                owner_name,
                "TXT",
                lifetime=timeout_seconds,
                search=False,
                raise_on_no_answer=False,
            )
        except Exception as exc:
            # dnspython exceptions are intentionally translated without leaking
            # resolver internals. NXDOMAIN and no-answer are ordinary no-proof.
            if type(exc).__name__ in {"NXDOMAIN", "NoAnswer"}:
                return ()
            raise DNSLookupFailure from exc

        rrset = answer.rrset
        if rrset is None:
            return ()
        if len(rrset) > max_records:
            raise DNSLookupFailure

        values: list[str] = []
        total_bytes = 0
        for record in rrset:
            chunks = getattr(record, "strings", None)
            if not isinstance(chunks, tuple) or not chunks:
                raise DNSLookupFailure
            joined = b"".join(chunks)
            total_bytes += len(joined)
            if total_bytes > max_response_bytes:
                raise DNSLookupFailure
            try:
                values.append(joined.decode("ascii", errors="strict"))
            except UnicodeDecodeError:
                # Non-ASCII TXT data cannot equal the ASCII challenge format.
                values.append("")
        return tuple(values)
