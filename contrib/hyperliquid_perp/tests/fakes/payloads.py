"""Venue payloads in the shape the Info endpoint answers with."""

from __future__ import annotations


def clearinghouse(
    *, account_value: str = "100", maintenance: str = "0", positions: list[dict] | None = None
) -> dict:
    return {
        "marginSummary": {
            "accountValue": account_value,
            "totalMarginUsed": "0",
            "totalNtlPos": "0",
        },
        "withdrawable": account_value,
        "crossMaintenanceMarginUsed": maintenance,
        "assetPositions": [{"position": p} for p in (positions or [])],
    }


def btc_position(szi: str = "0.001", upnl: str = "1", value: str = "51") -> dict:
    return {
        "coin": "BTC",
        "szi": szi,
        "entryPx": "50000",
        "unrealizedPnl": upnl,
        "positionValue": value,
        "marginUsed": value,
    }
