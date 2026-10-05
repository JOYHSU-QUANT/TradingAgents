"""A scripted anvil fork: one chain, its token balances and allowances, QuoterV2 and SwapRouter02.

Every transaction is mined as it is sent, one block each, as anvil mines by
default. A sent transaction is decoded from its signed bytes, so a test sees
what was signed (``sent``), and it acts on the fake's state as the real
contracts would for what this package sends: an ERC-20 ``approve``, and a
router ``multicall`` around one ``exactInputSingle`` or ``exactInput``.

A test steers it through its attributes: what the quoter and the swap
return, which calls a gas estimate reverts on, which mined transactions
revert, whether a send's answer is lost, and whether the node answers
``anvil_nodeInfo`` at all.
"""

from __future__ import annotations

from typing import Any

import requests
from eth_abi import decode, encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3

from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, QUOTER_V2, SWAP_ROUTER_02
from contrib.uniswap_v3.tests.fakes.rpc import ScriptedProvider, block_hash, block_result, encoded

__all__ = ["APPROVE_GAS", "SWAP_GAS", "FakeAnvil", "selector"]

APPROVE_GAS = 46_000
SWAP_GAS = 150_000
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


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


class FakeAnvil:
    def __init__(self, *, head: int = 100, timestamp: int = 1_700_000_000) -> None:
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
        self.quote_reverts: str | None = None
        # What a mined swap pays; ``None`` pays the quote.
        self.swap_out: int | None = None
        # Lowercase addresses a gas estimate reverts on, and ones a mined call reverts on.
        self.estimate_reverts: set[str] = set()
        self.mined_reverts: set[str] = set()
        self.node_info: dict[str, Any] | None = {
            "hardFork": "prague",
            "forkConfig": {"forkUrl": "https://node.example/v2/SECRET", "forkBlockNumber": 99},
        }
        # "mined": the send's answer is lost after it was mined; "dropped": before.
        self.lose_send: str | None = None
        # The error message a send is refused with, when it is.
        self.refuse_send: str | None = None
        # What a send is answered with in place of its hash, after it is mined.
        self.send_answer: dict[str, Any] | None = None
        # What a receipt read is answered with (an error response), or raises.
        self.receipt_error: dict[str, Any] | Exception | None = None
        # Whether sent transactions are mined; ``mine_only`` limits it to calls of those selectors.
        self.mine = True
        self.mine_only: set[bytes] | None = None
        # Whom a mined swap's output Transfer names as paid; ``None`` names its recipient.
        self.pay_to: str | None = None
        self.sent: list[dict[str, Any]] = []
        self.senders: list[str] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.provider = ScriptedProvider(self.respond)

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

    # --- the node --------------------------------------------------------------

    def respond(self, method: str, params: Any) -> dict[str, Any]:
        if method == "anvil_nodeInfo":
            if self.node_info is None:
                return {"error": {"code": -32601, "message": "Method not found"}}
            return {"result": self.node_info}
        if method == "eth_getBlockByNumber":
            if params[0] == "pending":
                return {
                    "result": block_result(self.head + 1, self._next_time(), base_fee=self.base_fee)
                }
            return {"result": block_result(self.head, self.timestamp, base_fee=self.base_fee)}
        if method == "eth_maxPriorityFeePerGas":
            return {"result": hex(self.tip)}
        if method == "eth_getTransactionCount":
            return {"result": hex(self.nonces.get(params[0].lower(), 0))}
        if method == "eth_call":
            return self._call(params[0])
        if method == "eth_estimateGas":
            return self._estimate(params[0])
        if method == "eth_sendRawTransaction":
            return self._send(params[0])
        if method == "eth_getTransactionReceipt":
            if isinstance(self.receipt_error, Exception):
                raise self.receipt_error
            if self.receipt_error is not None:
                return self.receipt_error
            return {"result": self.receipts.get(params[0].lower())}
        raise AssertionError(f"the fake anvil does not answer {method}")

    def _call(self, call: dict[str, Any]) -> dict[str, Any]:
        to, data = call["to"].lower(), bytes.fromhex(call["data"][2:])
        if to == _QUOTER:
            if self.quote_reverts is not None:
                return _revert(self.quote_reverts)
            if data[:4] == _QUOTE_SINGLE:
                return {"result": encoded(["uint256", "uint160", "uint32", "uint256"], [self.quote_out, 2**96, 1, 100_000])}
            assert data[:4] == _QUOTE_PATH
            path, _ = decode(["bytes", "uint256"], data[4:])
            hops = (len(path) - 20) // 23
            return {
                "result": encoded(
                    ["uint256", "uint160[]", "uint32[]", "uint256"],
                    [self.quote_out, [2**96] * hops, [1] * hops, 180_000],
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
        if self.refuse_send is not None:
            return {"error": {"code": -32000, "message": self.refuse_send}}
        if self.lose_send == "dropped":
            raise requests.ConnectionError("connection reset before the answer")
        self.sent.append(transaction)
        self.senders.append(sender)
        selector_sent = bytes(transaction["data"])[:4]
        if self.mine and (self.mine_only is None or selector_sent in self.mine_only):
            self._mine(tx_hash, sender, transaction)
        if self.lose_send == "mined":
            raise requests.ConnectionError("connection reset before the answer")
        if self.send_answer is not None:
            return self.send_answer
        return {"result": tx_hash}

    # --- mining ---------------------------------------------------------------

    def _next_time(self) -> int:
        return max(self.timestamp + 12, self.clock)

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
        out = self.quote_out if self.swap_out is None else self.swap_out
        allowed = self.allowance(token_in, owner, _ROUTER)
        if out < minimum or allowed < amount_in or self.balance(token_in, owner) < amount_in:
            return None
        self.allowances[(token_in, owner, _ROUTER)] = allowed - amount_in
        self.balances[(token_in, owner)] = self.balance(token_in, owner) - amount_in
        self.balances[(token_out, recipient)] = self.balance(token_out, recipient) + out
        return [
            {
                "address": Web3.to_checksum_address(token_in),
                "topics": [_TRANSFER, _topic(owner), _topic(_POOL)],
                "data": "0x" + f"{amount_in:064x}",
            },
            {
                "address": Web3.to_checksum_address(token_out),
                "topics": [_TRANSFER, _topic(_POOL), _topic(self.pay_to or recipient)],
                "data": "0x" + f"{out:064x}",
            },
        ]
