"""A gate-bound signed client scripted per cloid."""

from __future__ import annotations

from datetime import datetime

from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import CancelAck

from ..conftest import echo_order_status_cloid


class FakeSignedClient:
    """Mirrors the real client: the bound §4.1 gate judges every mutation.

    ``cancel_removes_order`` makes a successful cancel drop the order from
    ``open_orders_result`` and makes ``open_orders`` answer with a copy, for
    callers that re-read the listing afterwards.
    """

    def __init__(self, gate, clock=None, *, cancel_removes_order: bool = False):
        self._gate = gate
        self._clock = clock
        self._cancel_removes_order = cancel_removes_order
        # The exchange's clock. Defaults to agreeing with ours (zero skew), so
        # arm()'s skew guard passes; tests that care set it explicitly.
        self.exchange_time_result: datetime | None | Exception = None
        # orderStatus answers for the disarm cross-check. Default: the exchange
        # has never heard of the cloid.
        self.order_status_results: dict[str, object] = {}
        self.order_status_calls: list[str] = []
        # schedule_calls records only calls that SUCCEEDED; schedule_attempts
        # counts every one that reached the wire, which is what a test about
        # retry rate needs to see.
        self.schedule_calls: list[datetime] = []
        self.schedule_attempts: int = 0
        self.schedule_error: Exception | None = None
        # Seconds this call consumes before it answers, advanced on the manager's
        # own clock. The backoff is charged from the attempt's real duration, so a
        # test about it has to be able to say "this one burned the timeout".
        self.schedule_duration_s: float = 0
        self.clear_calls: int = 0
        self.clear_error: Exception | None = None
        self.open_orders_result: list | Exception = []
        self.open_orders_calls: int = 0
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_results: dict[str, CancelAck | Exception] = {}

    def schedule_cancel(self, *, cancel_at):
        self._gate.require_exchange_action(None)
        self.schedule_attempts += 1
        if self.schedule_duration_s and self._clock is not None:
            self._clock.advance(self.schedule_duration_s)
        if self.schedule_error is not None:
            raise self.schedule_error
        self.schedule_calls.append(cancel_at)

    def clear_scheduled_cancel(self):
        self._gate.require_exchange_action(None)
        if self.clear_error is not None:
            raise self.clear_error
        self.clear_calls += 1

    def open_orders(self):
        self.open_orders_calls += 1
        if isinstance(self.open_orders_result, Exception):
            raise self.open_orders_result
        if self._cancel_removes_order:
            return list(self.open_orders_result)
        return self.open_orders_result

    def exchange_time(self):
        if isinstance(self.exchange_time_result, Exception):
            raise self.exchange_time_result
        if self.exchange_time_result is not None:
            return self.exchange_time_result
        return None if self._clock is None else self._clock.now()

    def query_order_by_cloid(self, cloid_hex):
        self.order_status_calls.append(cloid_hex)
        result = self.order_status_results.get(cloid_hex, {"status": "unknownOid"})
        if isinstance(result, Exception):
            raise result
        return echo_order_status_cloid(result, cloid_hex)

    def cancel_by_cloid(self, *, coin, cloid_hex):
        self._gate.require_exchange_action(None)
        self.cancel_calls.append((coin, cloid_hex))
        result = self.cancel_results.get(cloid_hex, CancelAck(success=True))
        if isinstance(result, Exception):
            raise result
        if self._cancel_removes_order and result.success:
            self.open_orders_result = [
                o
                for o in self.open_orders_result
                # A non-dict entry (the exchange returning junk) is never the
                # order being cancelled, and must survive the filter — the fake
                # must not fail where the real client would not.
                if not isinstance(o, dict) or o.get("cloid") != cloid_hex
            ]
        return result
