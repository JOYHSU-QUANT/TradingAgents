"""The signing executor, against a scripted anvil: what it signs, what it fills, and what it refuses."""

from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from eth_abi import decode

from contrib.uniswap_v3.chain.errors import (
    ChainError,
    NotAFork,
    RpcConfigError,
    RpcRejected,
    SwapNotFilled,
    SwapOutcomeUnknown,
    TransactionUnconfirmed,
)
from contrib.uniswap_v3.chain.fork import DEV_ACCOUNTS, Fork
from contrib.uniswap_v3.chain.quoter import encode_path
from contrib.uniswap_v3.chain.swaps import ChainExecutor, SwapSettings
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, SWAP_ROUTER_02, TOKENS
from contrib.uniswap_v3.domain.records import FillSource
from contrib.uniswap_v3.domain.types import Bar, Fill, Rejection, SwapIntent, eth_from_wei
from contrib.uniswap_v3.ports import Executor
from contrib.uniswap_v3.tests.fakes.fork import (
    APPROVE_GAS,
    SWAP_GAS,
    FakeAnvil,
    selector,
    transfer,
)
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_ROUTER = SWAP_ROUTER_02[ETHEREUM_MAINNET]
_ME = DEV_ACCOUNTS[0]
_BAR = Bar(time=0, close_block=0, prices={"WETH": D(1)}, base_fee_wei=0)
# 1,000 USDC for 0.5 WETH, at least 0.49.
_SWAP = SwapIntent(token_in=_USDC, route=(_USDC_WETH,), amount_in=D(1000), min_amount_out=D("0.49"))
_PRICE = 10**9 + 10**8


def _anvil(*, usdc: int = 1000 * 10**6, quote: int = 5 * 10**17) -> FakeAnvil:
    anvil = FakeAnvil()
    anvil.fund(_USDC.address, _ME, usdc)
    anvil.quote_out = quote
    return anvil


def _executor(anvil: FakeAnvil, **kwargs) -> ChainExecutor:
    rpc, _ = rpc_over(anvil.provider)
    return ChainExecutor(Fork(rpc), sleep=lambda seconds: None, **kwargs)


def _multicall(sent: dict) -> tuple[int, bytes]:
    data = bytes(sent["data"])
    assert data[:4] == selector("multicall(uint256,bytes[])")
    deadline, (inner,) = decode(["uint256", "bytes[]"], data[4:])
    return deadline, inner


def test_a_swap_is_approved_exactly_then_sent_through_the_routers_multicall_and_filled():
    anvil = _anvil()
    executor = _executor(anvil)
    fill = executor.execute(_SWAP, _BAR)
    assert fill == Fill(
        swap=_SWAP,
        amount_out=D("0.5"),
        gas_cost_eth=eth_from_wei((APPROVE_GAS + SWAP_GAS) * _PRICE),
        block=102,
    )

    approve, swapped = anvil.sent
    assert anvil.senders == [_ME, _ME]
    # Signed for mainnet's chain ID, named in the transaction, with nonces in turn.
    assert [(tx["chainId"], tx["nonce"], tx["type"]) for tx in anvil.sent] == [(1, 0, 2), (1, 1, 2)]
    assert "0x" + bytes(approve["to"]).hex() == _USDC.address.lower()
    assert bytes(approve["data"])[:4] == selector("approve(address,uint256)")
    spender, amount = decode(["address", "uint256"], bytes(approve["data"])[4:])
    assert (spender, amount) == (_ROUTER.lower(), 1000 * 10**6)
    # The gas limit is the estimate and 20%; the fee cap twice the base fee and the tip.
    assert (approve["gas"], swapped["gas"]) == (55_200, 180_000)
    assert (swapped["maxFeePerGas"], swapped["maxPriorityFeePerGas"]) == (2 * 10**9 + 10**8, 10**8)

    # Every read the swap made was of the block it started from; every estimate was the wallet's.
    assert {params[1] for params in anvil.calls("eth_call")} == {hex(100)}
    assert {params[0]["from"].lower() for params in anvil.calls("eth_estimateGas")} == {_ME.lower()}
    deadline, inner = _multicall(swapped)
    # The pending block's time when the swap was sent (the approval's block, 12 s on) and 300 s.
    assert deadline == 1_700_000_024 + 300
    assert inner[:4] == selector(
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
    )
    (params,) = decode(["(address,address,uint24,address,uint256,uint256,uint160)"], inner[4:])
    assert params == (
        _USDC.address.lower(),
        _WETH.address.lower(),
        500,
        _ME.lower(),
        1000 * 10**6,
        49 * 10**16,
        0,
    )
    # Nothing is left approved, and the wallet holds what the fill says.
    assert anvil.allowance(_USDC.address, _ME, _ROUTER) == 0
    assert anvil.balance(_WETH.address, _ME) == 5 * 10**17


def test_a_two_hop_swap_is_one_exact_input_along_the_packed_path():
    anvil = _anvil(quote=3_000_000)
    swap = SwapIntent(
        token_in=_USDC,
        route=(_USDC_WETH, _WBTC_WETH),
        amount_in=D(1000),
        min_amount_out=D("0.0299"),
    )
    fill = _executor(anvil).execute(swap, _BAR)
    assert isinstance(fill, Fill) and fill.amount_out == D("0.03")
    _, inner = _multicall(anvil.sent[-1])
    assert inner[:4] == selector("exactInput((bytes,address,uint256,uint256))")
    ((path, recipient, amount_in, minimum),) = decode(["(bytes,address,uint256,uint256)"], inner[4:])
    assert path == encode_path(swap.tokens, swap.route)
    assert (recipient, amount_in, minimum) == (_ME.lower(), 1000 * 10**6, 2_990_000)


def _allow(anvil: FakeAnvil, raw: int) -> None:
    anvil.allow(_USDC.address, _ME, _ROUTER, raw)


def test_an_allowance_of_exactly_the_swap_sends_no_approval():
    anvil = _anvil()
    _allow(anvil, 1000 * 10**6)
    fill = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(fill, Fill) and fill.gas_cost_eth == eth_from_wei(SWAP_GAS * _PRICE)
    assert len(anvil.sent) == 1 and fill.block == 101


def test_a_larger_allowance_left_behind_is_set_back_to_the_swaps_amount():
    anvil = _anvil()
    _allow(anvil, 10**12)
    fill = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(fill, Fill) and len(anvil.sent) == 2
    assert anvil.allowance(_USDC.address, _ME, _ROUTER) == 0


def test_a_deadline_is_set_by_the_nodes_clock_and_not_by_a_latest_block_long_past():
    # An anvil left idle mines nothing: its latest block is an hour old, its clock is not.
    anvil = _anvil()
    _allow(anvil, 1000 * 10**6)
    anvil.clock = anvil.timestamp + 3_600
    fill = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(fill, Fill)
    deadline, _ = _multicall(anvil.sent[-1])
    assert deadline == anvil.clock + 300


@pytest.mark.parametrize(
    ("quote", "reverts", "said"),
    [
        (4 * 10**17, None, "below the swap's minimum"),
        (0, None, "below the swap's minimum"),
        (5 * 10**17, "SPL", "has no answer"),
    ],
)
def test_a_swap_the_quote_does_not_carry_is_refused_and_nothing_is_sent(quote, reverts, said):
    anvil = _anvil(quote=quote)
    anvil.quote_reverts = reverts
    refused = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(refused, Rejection) and said in refused.reason
    assert "nothing was sent" in refused.reason
    assert anvil.sent == [] and anvil.calls("eth_estimateGas") == []


def test_a_quote_quoterv2_gives_no_reason_for_is_the_nodes_problem_and_not_a_refusal():
    anvil = _anvil()
    anvil.quote_reverts = "Unexpected error"
    with pytest.raises(RpcRejected):
        _executor(anvil).execute(_SWAP, _BAR)
    assert anvil.sent == []


def test_a_wallet_that_holds_less_than_the_swap_sells_is_a_fault_and_not_a_refusal():
    anvil = _anvil(usdc=999 * 10**6)
    with pytest.raises(ValueError, match="holds 999 USDC at block 100, and the swap sells 1000 USDC"):
        _executor(anvil).execute(_SWAP, _BAR)
    assert anvil.sent == []


def test_a_swap_whose_estimate_reverts_before_anything_is_mined_is_refused():
    anvil = _anvil()
    _allow(anvil, 1000 * 10**6)
    anvil.estimate_reverts = {_ROUTER.lower()}
    refused = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(refused, Rejection)
    assert "would revert" in refused.reason and "nothing was sent" in refused.reason
    assert anvil.sent == []


def test_a_swap_whose_estimate_reverts_after_its_approval_was_mined_is_not_a_refusal():
    anvil = _anvil()
    anvil.estimate_reverts = {_ROUTER.lower()}
    with pytest.raises(SwapNotFilled, match="after its approval was mined") as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    [approval] = anvil.receipts
    assert caught.value.tx_hashes == (approval,)
    assert caught.value.gas_cost_eth == eth_from_wei(APPROVE_GAS * _PRICE)


def test_a_deadline_that_cannot_be_read_after_the_approval_was_mined_is_not_a_refusal():
    anvil = _anvil()
    anvil.pending_error = {"error": {"code": -32000, "message": "pending block unavailable"}}
    with pytest.raises(SwapNotFilled, match="not sent after its approval was mined") as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    assert caught.value.gas_cost_eth == eth_from_wei(APPROVE_GAS * _PRICE)
    assert len(anvil.sent) == 1


def test_a_swap_the_wallets_eth_cannot_pay_for_is_refused_while_nothing_is_mined():
    # An approval at most costs 55,200 gas at 2.1 gwei, 1.16e14 wei; a swap 3.78e14.
    anvil = _anvil()
    anvil.eth[_ME.lower()] = 10**14
    refused = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(refused, Rejection)
    assert refused.reason.startswith("the approval of 1000 USDC cannot be paid for: ")
    assert "nothing was sent" in refused.reason

    anvil = _anvil()
    _allow(anvil, 1000 * 10**6)
    anvil.eth[_ME.lower()] = 2 * 10**14
    refused = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(refused, Rejection) and "cannot be paid for" in refused.reason
    assert anvil.sent == []


def test_a_swap_the_wallets_eth_cannot_pay_for_after_its_approval_was_mined_is_not_a_refusal():
    anvil = _anvil()
    anvil.eth[_ME.lower()] = 2 * 10**14
    with pytest.raises(SwapNotFilled, match="InsufficientFunds") as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    assert caught.value.gas_cost_eth == eth_from_wei(APPROVE_GAS * _PRICE)


def test_anything_raised_after_the_approval_was_mined_names_the_approval(monkeypatch):
    anvil = _anvil()
    executor = _executor(anvil)
    # A deadline that cannot be added up: not an error of the chain's.
    monkeypatch.setattr(executor, "_settings", SimpleNamespace(deadline_seconds=None))
    with pytest.raises(SwapNotFilled, match="TypeError") as caught:
        executor.execute(_SWAP, _BAR)
    assert caught.value.tx_hashes == tuple(anvil.receipts)


def test_a_swap_mined_and_reverted_reports_the_gas_of_every_transaction_it_took():
    anvil = _anvil()
    anvil.mined_reverts = {_ROUTER.lower()}
    with pytest.raises(SwapNotFilled, match="was mined and reverted") as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    assert caught.value.tx_hashes == tuple(anvil.receipts)
    assert caught.value.gas_cost_eth == eth_from_wei((APPROVE_GAS + SWAP_GAS) * _PRICE)
    assert anvil.balance(_USDC.address, _ME) == 1000 * 10**6


def test_an_approval_whose_estimate_reverts_is_refused_and_nothing_is_sent():
    anvil = _anvil()
    anvil.estimate_reverts = {_USDC.address.lower()}
    refused = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(refused, Rejection)
    assert "approval of 1000 USDC would revert" in refused.reason
    assert "nothing was sent" in refused.reason and anvil.sent == []


def test_an_approval_mined_and_reverted_reports_its_gas():
    anvil = _anvil()
    anvil.mined_reverts = {_USDC.address.lower()}
    with pytest.raises(SwapNotFilled, match="approval of 1000 USDC reverted") as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    assert caught.value.gas_cost_eth == eth_from_wei(APPROVE_GAS * _PRICE)
    assert len(anvil.sent) == 1


def test_a_swap_whose_receipt_never_comes_names_every_transaction_sent():
    anvil = _anvil()
    # The approval is mined; the swap is taken and never mined.
    anvil.mine_only = {selector("approve(address,uint256)")}
    clock = iter(range(0, 1_000, 30))
    executor = ChainExecutor(
        Fork(rpc_over(anvil.provider)[0]), clock=lambda: next(clock), sleep=lambda s: None
    )
    with pytest.raises(TransactionUnconfirmed, match="no receipt came") as caught:
        executor.execute(_SWAP, _BAR)
    assert len(caught.value.tx_hashes) == 2
    assert caught.value.tx_hashes[0] in anvil.receipts
    assert caught.value.tx_hashes[1] not in anvil.receipts
    # The approval was mined, and its gas is known.
    assert caught.value.gas_cost_eth == eth_from_wei(APPROVE_GAS * _PRICE)


def test_only_the_output_tokens_transfers_to_the_wallet_count_and_they_add_up():
    anvil = _anvil()
    anvil.extra_logs = [
        # Another token paid to the wallet, another event of the output token, the output
        # token paid to someone else: none of them is the fill.
        transfer(_USDC.address, _ME, 10**6),
        transfer(_WETH.address, _ME, 10**17, topic0="0x" + "ee" * 32),
        transfer(_WETH.address, "0x" + "11" * 20, 10**17),
        # A second payment of the output token to the wallet: it is.
        transfer(_WETH.address, _ME, 2 * 10**16),
    ]
    fill = _executor(anvil).execute(_SWAP, _BAR)
    assert isinstance(fill, Fill) and fill.amount_out == D("0.52")


def test_a_receipt_paying_the_wallet_something_below_the_minimum_has_an_unknown_outcome():
    anvil = _anvil()
    anvil.pay_to = "0x" + "11" * 20
    anvil.extra_logs = [transfer(_WETH.address, _ME, 10**17)]
    with pytest.raises(SwapOutcomeUnknown, match="shows 0.1 WETH paid to the wallet, below"):
        _executor(anvil).execute(_SWAP, _BAR)


def test_a_swap_the_node_refuses_to_take_is_raised_as_it_is_and_not_a_refusal():
    # Nothing was mined and the node said why: not the market's answer, so not a Rejection.
    anvil = _anvil()
    _allow(anvil, 1000 * 10**6)
    anvil.send_before = {"error": {"code": -32000, "message": "nonce too low"}}
    with pytest.raises(RpcRejected, match="nonce too low"):
        _executor(anvil).execute(_SWAP, _BAR)
    assert anvil.receipts == {}


@pytest.mark.parametrize(
    ("pay_to", "swap_out", "said"),
    [
        # The output is paid to another address than the wallet, as far as the receipt says.
        ("0x" + "11" * 20, None, "shows 0 WETH paid to the wallet, below the swap's minimum"),
        # The output Transfer carries more than any token amount can be.
        (None, 2**256, "fits a uint256"),
    ],
)
def test_a_mined_swap_whose_receipt_cannot_be_read_as_a_fill_has_an_unknown_outcome(
    pay_to, swap_out, said
):
    anvil = _anvil()
    anvil.pay_to, anvil.swap_out = pay_to, swap_out
    # Mined and succeeded: tokens moved, so this is neither a read without an answer nor
    # a swap that only spent gas.
    with pytest.raises(SwapOutcomeUnknown, match=said) as caught:
        _executor(anvil).execute(_SWAP, _BAR)
    assert caught.value.tx_hashes == tuple(anvil.receipts)
    assert caught.value.gas_cost_eth == eth_from_wei((APPROVE_GAS + SWAP_GAS) * _PRICE)
    assert not isinstance(caught.value, SwapNotFilled | ChainError)


def test_the_executor_signs_on_a_fork_as_a_dev_account_and_is_an_executor():
    anvil = _anvil()
    executor = _executor(anvil, account=3)
    assert isinstance(executor, Executor)
    assert executor.source is FillSource.CHAIN
    assert executor.address == DEV_ACCOUNTS[3]
    with pytest.raises(ValueError, match="numbered 0 to 9"):
        _executor(anvil, account=10)


def test_the_executor_is_built_on_a_fork_and_asks_the_node_whether_it_is_anvil():
    anvil = _anvil()
    rpc, _ = rpc_over(anvil.provider)
    with pytest.raises(ValueError, match="built on a Fork"):
        ChainExecutor(rpc)  # type: ignore[arg-type]
    anvil.node_info = None
    with pytest.raises(NotAFork, match="anvil_nodeInfo"):
        ChainExecutor(Fork(rpc))
    assert anvil.sent == []


def test_a_chain_without_a_known_router_is_refused_when_the_executor_is_built():
    rpc, _ = rpc_over(FakeAnvil(chain_id=5).provider, chain_id=5)
    with pytest.raises(RpcConfigError, match="no SwapRouter02 address is known for chain 5"):
        ChainExecutor(Fork(rpc))


def test_the_wallet_balance_is_read_at_a_block():
    anvil = _anvil()
    assert _executor(anvil).balance(_USDC, block=100) == D(1000)


def test_swap_settings_refuse_a_deadline_that_cannot_work():
    for value in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="deadline_seconds"):
            SwapSettings(deadline_seconds=value)
    assert SwapSettings().deadline_seconds == 300
