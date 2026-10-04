"""web3 providers that never touch a network, and one that records a real one.

- :class:`ReplayProvider` answers from a cassette: a JSON list of
  ``{"method", "params", "response"}`` entries, looked up by method and
  params. A request that was not recorded fails the test.
- :class:`ScriptedProvider` hands every request to a function, which returns
  a response or raises.
- :class:`RecordingProvider` wraps a real provider and keeps what passed
  through it; ``tests/fixtures/record.py`` writes cassettes with it.

A cassette holds requests and responses only, never the endpoint URL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eth_abi import encode
from web3.providers.base import BaseProvider

from contrib.uniswap_v3.chain.rpc import Rpc, RpcSettings

# All the package reads off a block. A recorded block is cut down to these:
# its transaction list alone would be most of the cassette.
_HEADER_KEYS = ("number", "hash", "parentHash", "timestamp", "baseFeePerGas")


def block_result(number: int, timestamp: int, *, base_fee: int | None = 10**9) -> dict[str, Any]:
    """A block as a node sends it, cut down to the header; no base fee before London."""
    block = {
        "number": hex(number),
        "hash": "0x" + f"{number + 1:064x}",
        "parentHash": "0x" + f"{number:064x}",
        "timestamp": hex(timestamp),
    }
    if base_fee is not None:
        block["baseFeePerGas"] = hex(base_fee)
    return block


def encoded(types: list[str], values: list[Any]) -> str:
    """ABI-encoded ``values`` as the hex string an ``eth_call`` returns."""
    return "0x" + encode(types, values).hex()


def _key(method: str, params: Any) -> str:
    return json.dumps([method, params], sort_keys=True, default=str)


def _wrap(response: dict[str, Any]) -> Any:
    return {"jsonrpc": "2.0", "id": 1, **response}


class ReplayProvider(BaseProvider):
    def __init__(self, cassette: Path) -> None:
        super().__init__()
        entries = json.loads(cassette.read_text(encoding="utf-8"))
        self._responses = {_key(e["method"], e["params"]): e["response"] for e in entries}
        self.requests: list[tuple[str, Any]] = []

    def make_request(self, method: Any, params: Any) -> Any:
        self.requests.append((method, params))
        key = _key(method, params)
        if key not in self._responses:
            raise AssertionError(f"not in the cassette: {method} {params!r}")
        return _wrap(self._responses[key])


class ScriptedProvider(BaseProvider):
    def __init__(self, respond: Callable[[str, Any], dict[str, Any]]) -> None:
        super().__init__()
        self._respond = respond
        self.requests: list[tuple[str, Any]] = []

    def make_request(self, method: Any, params: Any) -> Any:
        self.requests.append((method, params))
        return _wrap(self._respond(method, params))


def answering(response: dict[str, Any]) -> ScriptedProvider:
    """A node that gives ``response`` to every request."""
    return ScriptedProvider(lambda method, params: response)


class RecordingProvider(BaseProvider):
    def __init__(self, inner: BaseProvider) -> None:
        super().__init__()
        self._inner = inner
        self._entries: dict[str, dict[str, Any]] = {}

    def make_request(self, method: Any, params: Any) -> Any:
        response = self._inner.make_request(method, params)
        kept = {name: response[name] for name in ("result", "error") if name in response}
        if method == "eth_getBlockByNumber" and isinstance(kept.get("result"), dict):
            block = kept["result"]
            kept["result"] = {name: block[name] for name in _HEADER_KEYS if name in block}
        self._entries[_key(method, params)] = {
            "method": method,
            "params": json.loads(json.dumps(params, default=str)),
            "response": kept,
        }
        return response

    def write(self, cassette: Path) -> None:
        text = json.dumps(list(self._entries.values()), indent=1) + "\n"
        cassette.write_text(text, encoding="utf-8", newline="\n")


def rpc_over(provider: BaseProvider, *, chain_id: int = 1, attempts: int = 3) -> tuple[Rpc, list]:
    """An :class:`Rpc` on ``provider`` whose waits are recorded instead of slept."""
    waits: list[float] = []
    settings = RpcSettings(attempts=attempts)
    return Rpc(provider, chain_id, settings=settings, sleep=waits.append), waits
