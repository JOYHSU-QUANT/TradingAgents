"""A scripted anvil fork: one chain, its token balances and allowances, QuoterV2 and SwapRouter02.

Every transaction is mined as it is sent (unless ``mine``/``mine_only`` say
otherwise), one block each, as anvil does. A sent transaction is decoded
from its signed bytes, so a test sees
what was signed (``sent``), and it acts on the fake's state as the real
contracts would for what this package sends: an ERC-20 ``approve``, and a
router ``multicall`` around one ``exactInputSingle`` or ``exactInput``.

A test steers it through its attributes: what the quoter and the swap
return, which calls a gas estimate reverts on, which mined transactions
revert, whether a send's answer is lost, and whether the node answers
``anvil_nodeInfo`` at all.

It also answers what a fork's wallet asks: ``anvil_reset`` (which drops
every balance, allowance and storage word and moves the head), an
account's ETH (``eth_getBalance``, ``anvil_setBalance``; a mined
transaction pays its gas from it) and a token's storage
(``eth_getStorageAt``, ``anvil_setStorageAt``), where a dev account's entry
of the token's balance mapping (:attr:`FakeAnvil.balance_slots`) is its
balance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eth_abi import decode, encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3

from contrib.uniswap_v3.chain.fork import DEV_ACCOUNTS
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, QUOTER_V2, SWAP_ROUTER_02, TOKENS
from contrib.uniswap_v3.tests.fakes.rpc import ScriptedProvider, block_hash, block_result, encoded

__all__ = [
    "APPROVE_GAS",
    "DEV_ETH",
    "FORK_URL",
    "SWAP_GAS",
    "FakeAnvil",
    "balance_key",
    "selector",
    "transfer",
]

APPROVE_GAS = 46_000
SWAP_GAS = 150_000
# What anvil gives each dev account, and gives again on a reset.
DEV_ETH = 10_000 * 10**18
# Where mainnet's three tokens keep their balances, as anvil finds them.
_BALANCE_SLOTS = {"USDC": 9, "WETH": 3, "WBTC": 0}
# The URL the fake says it was forked from.
FORK_URL = "https://upstream.invalid/fake-anvil-upstream-key"
_ROUTER = SWAP_ROUTER_02[ETHEREUM_MAINNET].lower()
_QUOTER = QUOTER_V2[ETHEREUM_MAINNET].lower()
_TRANSFER = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
# A pool's stand-in address in the Transfer events.
_POOL = "0x" + "77" * 20


def selector(signature: str) -> bytes:
    return bytes(Web3.keccak(text=signature))[:4]


_APPROVE = selector("approve(address,uint256)")
_BALANCE_OF = selector("balanceOf(address)")
_ALLOWANCE = selector("allowance(address,address)")
_MULTICALL = selector("multicall(uint256,bytes[])")
_SINGLE = selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
_PATH = selector("exactInput((bytes,address,uint256,uint256))")
_QUOTE_SINGLE = selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
_QUOTE_PATH = selector("quoteExactInput(bytes,uint256)")


def _revert(reason: str) -> dict[str, Any]:
    data = "0x08c379a0" + encode(["string"], [reason]).hex()
    return {"error": {"code": 3, "message": f"execution reverted: {reason}", "data": data}}


def _answer(answer: dict[str, Any] | Exception) -> dict[str, Any]:
    """``answer`` as the node's response: an error response returned, an exception raised."""
    if isinstance(answer, Exception):
        raise answer
    return answer


def transfer(
    token: str, to: str, raw: int, *, from_: str = _POOL, topic0: str | None = None
) -> dict[str, Any]:
    """A log of ``token``'s ``Transfer`` of ``raw`` from ``from_`` (a pool) to ``to``; ``topic0`` another event."""
    return {
        "address": Web3.to_checksum_address(token),
        "topics": [topic0 or _TRANSFER, _topic(from_), _topic(to)],
        "data": "0x" + f"{raw:064x}",
    }


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def balance_key(owner: str, slot: int) -> int:
    """Where ``owner``'s entry of a mapping declared at ``slot`` is stored, as an integer."""
    word = bytes(12) + bytes.fromhex(owner[2:]) + slot.to_bytes(32, "big")
    return int.from_bytes(Web3.keccak(word), "big")


class FakeAnvil:
    def __init__(
        self, *, head: int = 100, timestamp: int = 1_700_000_000, chain_id: int = 1
    ) -> None:
        self.head = head
        # The latest block's time, and the node's clock: the pending block, and the
        # next one mined, are at the later of the clock and twelve seconds on. An idle
        # anvil's clock runs ahead of its latest block.
        self.timestamp = timestamp
        self.clock = timestamp
        self.base_fee = 10**9
        self.tip = 10**8
        # (token, owner) and (token, owner, spender), all lowercase.
        self.balances: dict[tuple[str, str], int] = {}
        self.allowances: dict[tuple[str, str, str], int] = {}
        self.nonces: dict[str, int] = {}
        self.quote_out = 0
        # What a swap of (token in, token out, raw amount in), all lowercase, quotes and pays;
        # in place of ``quote_out`` when set.
        self.quote_for: Callable[[str, str, int], int] | None = None
        # The ETH each account holds, lowercase; a dev account not here holds DEV_ETH.
        self.eth: dict[str, int] = {}
        # Each token's balance mapping slot (lowercase address), and its other storage words.
        self.balance_slots: dict[str, int] = {
            TOKENS[ETHEREUM_MAINNET][symbol].address.lower(): slot
            for symbol, slot in _BALANCE_SLOTS.items()
        }
        self.storage: dict[tuple[str, int], int] = {}
        # The blocks the fork was reset to, oldest first.
        self.resets: list[int] = []
        self.quote_reverts: str | None = None
        # What a mined swap pays; ``None`` pays the quote.
        self.swap_out: int | None = None
        # Lowercase addresses a gas estimate reverts on, and ones a mined call reverts on.
        self.estimate_reverts: set[str] = set()
        self.mined_reverts: set[str] = set()
        self.node_info: dict[str, Any] | None = {
            "hardFork": "prague",
            # Its pieces become secrets for the whole test run: none may be text other tests read.
            "forkConfig": {"forkUrl": FORK_URL, "forkBlockNumber": 99},
        }
        # How a send is answered in place of its hash: an error response is returned, an
        # exception raised. ``send_before`` answers before the transaction is taken, which
        # is then neither recorded nor mined; ``send_answer`` after it is taken (and mined,
        # unless ``mine``/``mine_only`` say not).
        self.send_before: dict[str, Any] | Exception | None = None
        self.send_answer: dict[str, Any] | Exception | None = None
        # What a receipt read is answered with (an error response), or raises.
        self.receipt_error: dict[str, Any] | Exception | None = None
        # What a read of the pending block is answered with (an error response).
        self.pending_error: dict[str, Any] | None = None
        # Logs a mined swap emits after its own two Transfers.
        self.extra_logs: list[dict[str, Any]] = []
        # Whether sent transactions are mined; ``mine_only`` limits it to calls of those selectors.
        self.mine = True
        self.mine_only: set[bytes] | None = None
        # Whom a mined swap's output Transfer names as paid; ``None`` names its recipient.
        self.pay_to: str | None = None
        self.sent: list[dict[str, Any]] = []
        self.senders: list[str] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.provider = ScriptedProvider(self.respond, chain_id=chain_id)

    # --- what a test sets up and reads ---------------------------------------

    def fund(self, token: str, owner: str, raw: int) -> None:
        self.balances[(token.lower(), owner.lower())] = raw

    def allow(self, token: str, owner: str, spender: str, raw: int) -> None:
        self.allowances[(token.lower(), owner.lower(), spender.lower())] = raw

    def balance(self, token: str, owner: str) -> int:
        return self.balances.get((token.lower(), owner.lower()), 0)

    def allowance(self, token: str, owner: str, spender: str) -> int:
        return self.allowances.get((token.lower(), owner.lower(), spender.lower()), 0)

    def calls(self, method: str) -> list[Any]:
        return [params for name, params in self.provider.requests if name == method]

    def eth_of(self, owner: str) -> int:
        return self.eth.get(owner.lower(), DEV_ETH)

    def _balance_owner(self, token: str, key: int) -> str | None:
        """The dev account whose balance of ``token`` is stored at ``key``, when one's is."""
        slot = self.balance_slots.get(token)
        if slot is None:
            return None
        for owner in DEV_ACCOUNTS:
            if balance_key(owner, slot) == key:
                return owner.lower()
        return None

    def _storage(self, token: str, key: int) -> int:
        owner = self._balance_owner(token, key)
        return self.balance(token, owner) if owner else self.storage.get((token, key), 0)

    def _reset(self, block: int) -> None:
        self.resets.append(block)
        self.head = block
        self.balances.clear()
        self.allowances.clear()
        self.storage.clear()
        self.eth.clear()
        if self.node_info is not None:
            self.node_info["forkConfig"]["forkBlockNumber"] = block

    # --- the node --------------------------------------------------------------

    def respond(self, method: str, params: Any) -> dict[str, Any]:
        if method == "anvil_nodeInfo":
            if self.node_info is None:
                return {"error": {"code": -32601, "message": "Method not found"}}
            return {"result": self.node_info}
        if method == "eth_getBlockByNumber":
            if params[0] == "pending":
                if self.pending_error is not None:
                    return self.pending_error
                return {
                    "result": block_result(self.head + 1, self._next_time(), base_fee=self.base_fee)
                }
            return {"result": block_result(self.head, self.timestamp, base_fee=self.base_fee)}
        if method == "eth_maxPriorityFeePerGas":
            return {"result": hex(self.tip)}
        if method == "anvil_reset":
            self._reset(params[0]["forking"]["blockNumber"])
            return {"result": None}
        if method == "anvil_setBalance":
            self.eth[params[0].lower()] = int(params[1], 16)
            return {"result": None}
        if method == "anvil_setStorageAt":
            token, key, value = params[0].lower(), int(params[1], 16), int(params[2], 16)
            owner = self._balance_owner(token, key)
            if owner:
                self.fund(token, owner, value)
            else:
                self.storage[(token, key)] = value
            return {"result": True}
        if method == "eth_getStorageAt":
            word = self._storage(params[0].lower(), int(params[1], 16))
            return {"result": "0x" + f"{word:064x}"}
        if method == "eth_getBalance":
            return {"result": hex(self.eth_of(params[0]))}
        if method == "eth_getTransactionCount":
            return {"result": hex(self.nonces.get(params[0].lower(), 0))}
        if method == "eth_call":
            return self._call(params[0])
        if method == "eth_estimateGas":
            return self._estimate(params[0])
        if method == "eth_sendRawTransaction":
            return self._send(params[0])
        if method == "eth_getTransactionReceipt":
            if self.receipt_error is not None:
                return _answer(self.receipt_error)
            return {"result": self.receipts.get(params[0].lower())}
        raise AssertionError(f"the fake anvil does not answer {method}")

    def _call(self, call: dict[str, Any]) -> dict[str, Any]:
        to, data = call["to"].lower(), bytes.fromhex(call["data"][2:])
        if to == _QUOTER:
            if self.quote_reverts is not None:
                return _revert(self.quote_reverts)
            if data[:4] == _QUOTE_SINGLE:
                ((token_in, token_out, amount_in, _, _),) = decode(
                    ["(address,address,uint256,uint24,uint160)"], data[4:]
                )
                out = self._out(token_in, token_out, amount_in)
                return {"result": encoded(["uint256", "uint160", "uint32", "uint256"], [out, 2**96, 1, 100_000])}
            assert data[:4] == _QUOTE_PATH
            path, amount_in = decode(["bytes", "uint256"], data[4:])
            hops = (len(path) - 20) // 23
            out = self._out("0x" + path[:20].hex(), "0x" + path[-20:].hex(), amount_in)
            return {
                "result": encoded(
                    ["uint256", "uint160[]", "uint32[]", "uint256"],
                    [out, [2**96] * hops, [1] * hops, 180_000],
                )
            }
        if data[:4] == _BALANCE_OF:
            (owner,) = decode(["address"], data[4:])
            return {"result": encoded(["uint256"], [self.balance(to, owner)])}
        assert data[:4] == _ALLOWANCE, data[:4]
        owner, spender = decode(["address", "address"], data[4:])
        return {"result": encoded(["uint256"], [self.allowance(to, owner, spender)])}

    def _estimate(self, call: dict[str, Any]) -> dict[str, Any]:
        if call["to"].lower() in self.estimate_reverts:
            return _revert("Too little received")
        gas = APPROVE_GAS if bytes.fromhex(call["data"][2:])[:4] == _APPROVE else SWAP_GAS
        return {"result": hex(gas)}

    def _send(self, raw_hex: str) -> dict[str, Any]:
        raw = HexBytes(raw_hex)
        transaction = TypedTransaction.from_bytes(raw).as_dict()
        sender = Account.recover_transaction(raw)
        tx_hash = "0x" + bytes(Web3.keccak(raw)).hex()
        if self.send_before is not None:
            return _answer(self.send_before)
        self.sent.append(transaction)
        self.senders.append(sender)
        selector_sent = bytes(transaction["data"])[:4]
        if self.mine and (self.mine_only is None or selector_sent in self.mine_only):
            self._mine(tx_hash, sender, transaction)
        if self.send_answer is not None:
            return _answer(self.send_answer)
        return {"result": tx_hash}

    # --- mining ---------------------------------------------------------------

    def _next_time(self) -> int:
        return max(self.timestamp + 12, self.clock)

    def _out(self, token_in: str, token_out: str, amount_in: int) -> int:
        """What a swap of ``amount_in`` of ``token_in`` for ``token_out`` quotes, and pays."""
        if self.quote_for is None:
            return self.quote_out
        return self.quote_for(token_in.lower(), token_out.lower(), amount_in)

    def _mine(self, tx_hash: str, sender: str, transaction: dict[str, Any]) -> None:
        self.head += 1
        self.timestamp = self._next_time()
        owner = sender.lower()
        self.nonces[owner] = self.nonces.get(owner, 0) + 1
        to = "0x" + bytes(transaction["to"]).hex()
        data = bytes(transaction["data"])
        logs: list[dict[str, Any]] = []
        succeeded = to not in self.mined_reverts
        if data[:4] == _APPROVE:
            gas = APPROVE_GAS
            if succeeded:
                spender, amount = decode(["address", "uint256"], data[4:])
                self.allowances[(to, owner, spender.lower())] = amount
        else:
            assert to == _ROUTER and data[:4] == _MULTICALL, (to, data[:4])
            gas = SWAP_GAS
            deadline, (inner,) = decode(["uint256", "bytes[]"], data[4:])
            succeeded = succeeded and deadline >= self.timestamp
            if succeeded:
                logs = self._swap(owner, inner)
                succeeded = logs is not None
        self.eth[owner] = self.eth_of(owner) - gas * (self.base_fee + self.tip)
        self.receipts[tx_hash] = {
            "transactionHash": tx_hash,
            "transactionIndex": "0x0",
            "blockHash": block_hash(self.head),
            "blockNumber": hex(self.head),
            "from": sender,
            "to": Web3.to_checksum_address(to),
            "cumulativeGasUsed": hex(gas),
            "gasUsed": hex(gas),
            "effectiveGasPrice": hex(self.base_fee + self.tip),
            "contractAddress": None,
            "logs": [
                {**log, "logIndex": hex(index), "blockNumber": hex(self.head), "transactionHash": tx_hash}
                for index, log in enumerate(logs or [])
            ],
            "logsBloom": "0x" + "00" * 256,
            "status": "0x1" if succeeded else "0x0",
            "type": "0x2",
        }

    def _swap(self, owner: str, inner: bytes) -> list[dict[str, Any]] | None:
        """The router's swap for ``owner``: its Transfer events, or ``None`` when it reverts."""
        if inner[:4] == _SINGLE:
            ((token_in, token_out, _, recipient, amount_in, minimum, _),) = decode(
                ["(address,address,uint24,address,uint256,uint256,uint160)"], inner[4:]
            )
        else:
            assert inner[:4] == _PATH
            ((path, recipient, amount_in, minimum),) = decode(
                ["(bytes,address,uint256,uint256)"], inner[4:]
            )
            token_in, token_out = "0x" + path[:20].hex(), "0x" + path[-20:].hex()
        token_in, token_out, recipient = token_in.lower(), token_out.lower(), recipient.lower()
        out = self._out(token_in, token_out, amount_in) if self.swap_out is None else self.swap_out
        allowed = self.allowance(token_in, owner, _ROUTER)
        if out < minimum or allowed < amount_in or self.balance(token_in, owner) < amount_in:
            return None
        self.allowances[(token_in, owner, _ROUTER)] = allowed - amount_in
        self.balances[(token_in, owner)] = self.balance(token_in, owner) - amount_in
        self.balances[(token_out, recipient)] = self.balance(token_out, recipient) + out
        return [
            transfer(token_in, _POOL, amount_in, from_=owner),
            transfer(token_out, self.pay_to or recipient, out),
            *self.extra_logs,
        ]
