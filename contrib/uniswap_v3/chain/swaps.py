"""The executor that signs: swaps sent to SwapRouter02 from an anvil dev account on a local fork.

A :class:`ChainExecutor` is built on a :class:`~.fork.Fork` and nothing
else, and signs with one of anvil's public dev accounts. A live chain has
no way in: that is a later phase, with its own guard.

One swap, from first to last:

1. It is quoted by QuoterV2 at the latest block. A swap the pools give no
   answer for, or whose quote is below its ``min_amount_out``, is refused
   (:class:`~..domain.types.Rejection`), and nothing is sent.
2. The wallet must hold what the swap sells. One that does not is not the
   market's answer but a wallet out of step with its ledger, and raises
   ``ValueError``.
3. When the router may not take that much of the token yet, an ERC-20
   approval of exactly the amount is sent first. Nothing is approved
   beyond what one swap sells.
4. The swap is sent to SwapRouter02 inside ``multicall(deadline, ...)``,
   with ``amountOutMinimum`` the swap's ``min_amount_out``: the same floor
   every executor holds a swap to. The deadline is the latest block's time
   plus :attr:`SwapSettings.deadline_seconds`. A swap whose gas estimate
   says it would revert is refused while nothing has been sent.
5. The fill is what the receipt's ``Transfer`` events of the output token
   paid the wallet, in the block the swap was mined in, and its gas is
   that of every transaction the swap took: the approval's and its own, at
   the price each paid. A receipt that shows less than the minimum paid is
   not taken for a fill.

:class:`~..domain.types.Rejection` means the wallet is as it was. Once a
transaction has been mined, a swap that does not fill raises
:class:`~.errors.SwapNotFilled` with the gas spent, and one whose receipt
never came :class:`~.errors.TransactionUnconfirmed`: the wallet changed, or
may yet change.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from eth_abi import encode
from eth_abi.grammar import parse
from web3 import Web3

from ..constants import SWAP_ROUTER_02
from ..domain.decimal_context import plain
from ..domain.records import FillSource
from ..domain.types import Bar, Fill, Rejection, SwapIntent, Token, eth_from_wei
from ..ports import NoQuote
from .errors import (
    CallReverted,
    ChainError,
    RpcConfigError,
    SwapNotFilled,
    TransactionReverted,
    TransactionUnconfirmed,
)
from .fork import Fork, dev_account, require_anvil
from .quoter import ChainQuoter, encode_path
from .rpc import Receipt
from .transactions import SendSettings, TransactionSender
from .units import from_raw, to_raw

__all__ = ["ChainExecutor", "SwapSettings"]

# The two ERC-20 reads a swap needs.
_ERC20_ABI: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
)
_TRANSFER_TOPIC: Final = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
# SwapRouter02's IV3SwapRouter structs, which (unlike the first SwapRouter's)
# carry no deadline; the deadline is multicall's:
# https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IV3SwapRouter.sol
# https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/interfaces/IMulticallExtended.sol
_EXACT_INPUT_SINGLE: Final = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
_EXACT_INPUT: Final = "exactInput((bytes,address,uint256,uint256))"
_MULTICALL: Final = "multicall(uint256,bytes[])"
_APPROVE: Final = "approve(address,uint256)"


def _calldata(signature: str, values: list[Any]) -> bytes:
    """``signature``'s selector and ``values`` ABI-encoded by the argument types it names."""
    arguments = parse(signature[signature.index("(") :])
    types = [component.to_type_str() for component in arguments.components]
    return bytes(Web3.keccak(text=signature))[:4] + encode(types, values)


def _topic_address(address: str) -> str:
    """``address`` as an indexed event argument: left-padded to 32 bytes, lowercase."""
    return "0x" + "0" * 24 + address[2:].lower()


def _gas_eth(receipts: list[Receipt], extra_wei: int = 0) -> Decimal:
    return eth_from_wei(sum(receipt.gas_cost_wei for receipt in receipts) + extra_wei)


def _hashes(receipts: list[Receipt]) -> tuple[str, ...]:
    return tuple(receipt.tx_hash for receipt in receipts)


@dataclass(frozen=True)
class SwapSettings:
    """``deadline_seconds``: how long after the latest block's time a sent swap may still be mined."""

    deadline_seconds: int = 300

    def __post_init__(self) -> None:
        value = self.deadline_seconds
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"deadline_seconds must be an integer of at least 1, got {value!r}")


class ChainExecutor:
    """An :class:`~..ports.Executor` that signs and sends each swap on a local anvil fork.

    Built on ``fork``, which :func:`~.fork.open_fork` returned, it asks the
    node ``anvil_nodeInfo`` once more and refuses one that does not answer.
    It signs as anvil's dev account number ``account``.
    """

    source: Final = FillSource.CHAIN

    def __init__(
        self,
        fork: Fork,
        *,
        account: int = 0,
        settings: SwapSettings | None = None,
        send: SendSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(fork, Fork):
            raise ValueError(f"a chain executor is built on a Fork, got {fork!r}")
        rpc = fork.rpc
        require_anvil(rpc)
        if rpc.chain_id not in SWAP_ROUTER_02:
            raise RpcConfigError(f"no SwapRouter02 address is known for chain {rpc.chain_id}")
        self._rpc = rpc
        self._router = SWAP_ROUTER_02[rpc.chain_id]
        self._quoter = ChainQuoter(rpc)
        self._settings = settings if settings is not None else SwapSettings()
        self._sender = TransactionSender(
            rpc, dev_account(account), settings=send, clock=clock, sleep=sleep
        )

    @property
    def address(self) -> str:
        """The wallet the swaps are signed by, sold from and paid to."""
        return self._sender.address

    def balance(self, token: Token, *, block: int) -> Decimal:
        """What the wallet holds of ``token`` at the end of ``block``."""
        return from_raw(token, self._raw_balance(token, block))

    def _raw_balance(self, token: Token, block: int) -> int:
        return int(
            self._rpc.call(token.address, _ERC20_ABI, "balanceOf", (self.address,), block=block)
        )

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        """Sign and send ``swap``, and fill it with what the chain paid; ``bar`` is not consulted.

        A swap is refused only while nothing has been sent.
        """
        rpc, token_in, token_out = self._rpc, swap.token_in, swap.token_out
        raw_in = to_raw(token_in, swap.amount_in)
        head = rpc.latest_header()
        selling = f"{plain(swap.amount_in)} {token_in.symbol}"
        try:
            quoted, _ = self._quoter.quote(token_in, swap.route, swap.amount_in, block=head.number)
        except NoQuote as exc:
            return Rejection(
                swap, f"the quote at block {head.number} has no answer ({exc}); nothing was sent"
            )
        if quoted == 0 or quoted < swap.min_amount_out:
            return Rejection(
                swap,
                f"the quote of {plain(quoted)} {token_out.symbol} at block {head.number} is "
                f"below the swap's minimum of {plain(swap.min_amount_out)}; nothing was sent",
            )
        held = self._raw_balance(token_in, head.number)
        if held < raw_in:
            raise ValueError(
                f"the wallet {self.address} holds {plain(from_raw(token_in, held))} "
                f"{token_in.symbol} at block {head.number}, and the swap sells {selling}"
            )

        mined: list[Receipt] = []
        allowance = rpc.call(
            token_in.address,
            _ERC20_ABI,
            "allowance",
            (self.address, self._router),
            block=head.number,
        )
        if allowance < raw_in:
            approve = _calldata(_APPROVE, [self._router, raw_in])
            try:
                mined.append(
                    self._sender.send(token_in.address, approve, what=f"approving {selling}")
                )
            except TransactionReverted as exc:
                raise SwapNotFilled(
                    f"the approval of {selling} reverted ({exc})",
                    tx_hashes=exc.tx_hashes,
                    gas_cost_eth=eth_from_wei(exc.gas_cost_wei),
                ) from exc

        data = _calldata(
            _MULTICALL,
            [head.timestamp + self._settings.deadline_seconds, [self._swap_call(swap, raw_in)]],
        )
        what = f"the swap of {selling} for {token_out.symbol}"
        try:
            swapped = self._sender.send(self._router, data, what=what)
        except TransactionReverted as exc:
            raise SwapNotFilled(
                f"{what} was mined and reverted ({exc})",
                tx_hashes=_hashes(mined) + exc.tx_hashes,
                gas_cost_eth=_gas_eth(mined, exc.gas_cost_wei),
            ) from exc
        except TransactionUnconfirmed as exc:
            raise TransactionUnconfirmed(
                str(exc), tx_hashes=_hashes(mined) + exc.tx_hashes
            ) from exc
        except ChainError as exc:
            if mined:
                raise SwapNotFilled(
                    f"{what} was not sent after its approval was mined ({exc})",
                    tx_hashes=_hashes(mined),
                    gas_cost_eth=_gas_eth(mined),
                ) from exc
            if isinstance(exc, CallReverted):
                return Rejection(swap, f"{what} would revert ({exc}); nothing was sent")
            raise
        mined.append(swapped)

        amount_out = from_raw(token_out, self._paid(swapped, token_out))
        if amount_out == 0 or amount_out < swap.min_amount_out:
            # The router holds a swap to its minimum, so the receipt is not what was asked
            # for; the swap was mined all the same, and its gas spent.
            raise SwapNotFilled(
                f"{what} ({swapped.tx_hash}) was mined, and its receipt shows "
                f"{plain(amount_out)} {token_out.symbol} paid to the wallet, "
                f"below the swap's minimum of {plain(swap.min_amount_out)}",
                tx_hashes=_hashes(mined),
                gas_cost_eth=_gas_eth(mined),
            )
        return Fill(
            swap=swap, amount_out=amount_out, gas_cost_eth=_gas_eth(mined), block=swapped.block
        )

    def _swap_call(self, swap: SwapIntent, raw_in: int) -> bytes:
        """The router call that sells ``raw_in`` of the swap's input along its route."""
        raw_min = to_raw(swap.token_out, swap.min_amount_out)
        if len(swap.route) == 1:
            return _calldata(
                _EXACT_INPUT_SINGLE,
                [
                    (
                        swap.token_in.address,
                        swap.token_out.address,
                        swap.route[0].fee,
                        self.address,
                        raw_in,
                        raw_min,
                        0,
                    )
                ],
            )
        path = encode_path(swap.tokens, swap.route)
        return _calldata(_EXACT_INPUT, [(path, self.address, raw_in, raw_min)])

    def _paid(self, receipt: Receipt, token: Token) -> int:
        """What ``receipt``'s ``Transfer`` events of ``token`` paid the wallet, in raw units."""
        to = _topic_address(self.address)
        paid = 0
        for log in receipt.logs:
            if (
                log.address == token.address.lower()
                and len(log.topics) == 3
                and log.topics[0] == _TRANSFER_TOPIC
                and log.topics[2] == to
            ):
                paid += int(log.data, 16) if log.data != "0x" else 0
        return paid
