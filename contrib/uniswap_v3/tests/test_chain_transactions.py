"""Signing, sending once and waiting: the sender against a scripted anvil, and the reads it is built on."""

from __future__ import annotations

import pytest
import requests
from eth_abi import encode
from eth_account import Account

from contrib.uniswap_v3.chain.errors import (
    CallReverted,
    MalformedResponse,
    RpcRejected,
    RpcUnavailable,
    TransactionReverted,
    TransactionUnconfirmed,
)
from contrib.uniswap_v3.chain.fork import dev_account
from contrib.uniswap_v3.chain.rpc import Log, Receipt, _receipt as _parse_receipt
from contrib.uniswap_v3.chain.transactions import SendSettings, TransactionSender
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, SWAP_ROUTER_02, TOKENS
from contrib.uniswap_v3.tests.fakes.fork import APPROVE_GAS, FakeAnvil, selector
from contrib.uniswap_v3.tests.fakes.rpc import ScriptedProvider, answering, rpc_over

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"].address
_APPROVE = selector("approve(address,uint256)") + encode(
    ["address", "uint256"], [SWAP_ROUTER_02[ETHEREUM_MAINNET], 5]
)
_HASH = "0x" + "ab" * 32


def _sender(anvil: FakeAnvil, **settings) -> tuple[TransactionSender, list[float]]:
    rpc, _ = rpc_over(anvil.provider)
    slept: list[float] = []
    clock = iter(range(0, 10_000, 10))
    sender = TransactionSender(
        rpc,
        dev_account(0),
        settings=SendSettings(**settings),
        clock=lambda: next(clock),
        sleep=slept.append,
    )
    return sender, slept


def test_a_transaction_is_estimated_signed_for_the_chain_sent_once_and_its_receipt_returned():
    anvil = FakeAnvil()
    sender, slept = _sender(anvil)
    receipt = sender.send(_USDC, _APPROVE, what="an approval")
    assert receipt.succeeded and receipt.block == 101
    assert receipt.gas_cost_wei == APPROVE_GAS * (10**9 + 10**8)
    [sent] = anvil.sent
    assert (sent["chainId"], sent["nonce"], sent["type"], sent["value"]) == (1, 0, 2, 0)
    assert sent["gas"] == APPROVE_GAS * 120 // 100
    assert len(anvil.calls("eth_sendRawTransaction")) == 1
    # The node's next nonce is the pending one.
    assert anvil.calls("eth_getTransactionCount") == [[sender.address, "pending"]]
    assert slept == []


def test_a_call_whose_estimate_reverts_is_not_sent():
    anvil = FakeAnvil()
    anvil.estimate_reverts = {_USDC.lower()}
    sender, _ = _sender(anvil)
    with pytest.raises(CallReverted, match="Too little received"):
        sender.send(_USDC, _APPROVE, what="an approval")
    assert anvil.calls("eth_sendRawTransaction") == []


def test_a_send_the_node_refuses_is_raised_as_the_node_said_and_not_retried():
    anvil = FakeAnvil()
    anvil.refuse_send = "nonce too low"
    sender, _ = _sender(anvil)
    with pytest.raises(RpcRejected, match="nonce too low"):
        sender.send(_USDC, _APPROVE, what="an approval")
    assert len(anvil.calls("eth_sendRawTransaction")) == 1


def test_a_send_whose_answer_is_lost_after_it_was_mined_goes_on_to_its_receipt():
    anvil = FakeAnvil()
    anvil.lose_send = "mined"
    sender, _ = _sender(anvil)
    receipt = sender.send(_USDC, _APPROVE, what="an approval")
    assert receipt.succeeded
    assert len(anvil.calls("eth_sendRawTransaction")) == 1


def test_a_send_whose_answer_is_lost_before_it_arrived_is_unconfirmed_and_not_sent_again():
    anvil = FakeAnvil()
    anvil.lose_send = "dropped"
    sender, _ = _sender(anvil)
    with pytest.raises(TransactionUnconfirmed, match="may or may not have reached") as caught:
        sender.send(_USDC, _APPROVE, what="an approval")
    assert len(caught.value.tx_hashes) == 1
    assert len(anvil.calls("eth_sendRawTransaction")) == 1


def test_a_lost_send_whose_receipt_cannot_be_read_says_both():
    anvil = FakeAnvil()
    anvil.lose_send = "dropped"
    anvil.receipt_error = {"error": {"code": -32000, "message": "receipts are down"}}
    sender, _ = _sender(anvil)
    with pytest.raises(TransactionUnconfirmed, match="could not be read either .*receipts are down"):
        sender.send(_USDC, _APPROVE, what="an approval")


def test_a_send_answered_with_another_hash_is_unconfirmed_naming_both():
    anvil = FakeAnvil()
    other = "0x" + "cd" * 32
    anvil.send_answer = {"result": other}
    sender, _ = _sender(anvil)
    with pytest.raises(TransactionUnconfirmed, match="the node took it as") as caught:
        sender.send(_USDC, _APPROVE, what="an approval")
    [signed] = anvil.receipts
    assert caught.value.tx_hashes == (signed, other)


@pytest.mark.parametrize(
    "answer",
    [
        {"result": "0x1234"},
        # A refusal, and yet the node took the transaction: it is mined.
        {"error": {"code": -32000, "message": "already known"}},
    ],
)
def test_a_send_answered_with_something_else_goes_on_to_the_receipt_it_has(answer):
    anvil = FakeAnvil()
    anvil.send_answer = answer
    sender, _ = _sender(anvil)
    receipt = sender.send(_USDC, _APPROVE, what="an approval")
    assert receipt.succeeded and receipt.tx_hash in anvil.receipts
    assert len(anvil.calls("eth_sendRawTransaction")) == 1


def test_only_an_anvil_dev_account_is_signed_with():
    rpc, _ = rpc_over(FakeAnvil().provider)
    stranger = Account.from_key("0x" + "42" * 32)
    with pytest.raises(ValueError, match=f"{stranger.address} is not one"):
        TransactionSender(rpc, stranger)


def test_a_transaction_mined_and_reverted_raises_with_its_gas():
    anvil = FakeAnvil()
    anvil.mined_reverts = {_USDC.lower()}
    sender, _ = _sender(anvil)
    with pytest.raises(TransactionReverted, match="was mined in block 101 and reverted") as caught:
        sender.send(_USDC, _APPROVE, what="an approval")
    assert caught.value.gas_cost_wei == APPROVE_GAS * (10**9 + 10**8)
    assert caught.value.tx_hashes == tuple(anvil.receipts)


def test_a_receipt_that_does_not_come_in_time_is_unconfirmed_after_polling():
    anvil = FakeAnvil()
    anvil.mine = False
    sender, slept = _sender(anvil, receipt_timeout_seconds=30, poll_seconds=2)
    with pytest.raises(TransactionUnconfirmed, match="no receipt came in 30 seconds"):
        sender.send(_USDC, _APPROVE, what="an approval")
    # The clock moves 10 seconds a read: asked at 10, 20 and 30, slept after the first two.
    assert slept == [2, 2]


def test_a_receipt_that_cannot_be_read_after_the_send_is_unconfirmed():
    anvil = FakeAnvil()
    anvil.receipt_error = requests.ConnectionError("refused")
    sender, _ = _sender(anvil)
    with pytest.raises(TransactionUnconfirmed, match="could not be read"):
        sender.send(_USDC, _APPROVE, what="an approval")
    assert len(anvil.sent) == 1


def test_a_head_without_a_base_fee_sends_nothing():
    anvil = FakeAnvil()
    anvil.base_fee = None
    sender, _ = _sender(anvil)
    with pytest.raises(MalformedResponse, match="no base fee"):
        sender.send(_USDC, _APPROVE, what="an approval")
    assert anvil.sent == []


@pytest.mark.parametrize(
    "settings",
    [
        {"gas_margin_percent": -1},
        {"gas_margin_percent": 1.5},
        {"receipt_timeout_seconds": 0},
        {"poll_seconds": -1},
        {"poll_seconds": True},
        {"poll_seconds": float("inf")},
        {"receipt_timeout_seconds": float("nan")},
    ],
)
def test_send_settings_refuse_values_that_cannot_work(settings):
    with pytest.raises(ValueError):
        SendSettings(**settings)


# --- the reads a sender is built on ------------------------------------------


def test_the_send_is_tried_once_even_on_a_failure_a_read_would_retry():
    calls = []

    def respond(method, params):
        calls.append(method)
        raise requests.ConnectionError("refused")

    rpc, waits = rpc_over(ScriptedProvider(respond))
    with pytest.raises(RpcUnavailable, match="after 1 attempt"):
        rpc.send_raw_transaction(b"\x02\x01")
    assert calls == ["eth_sendRawTransaction"] and waits == []


def _receipt(**changes) -> dict:
    receipt = {
        "transactionHash": _HASH,
        "blockNumber": "0x7",
        "blockHash": "0x" + "01" * 32,
        "transactionIndex": "0x0",
        "status": "0x1",
        "gasUsed": "0x10",
        "cumulativeGasUsed": "0x10",
        "effectiveGasPrice": "0x3",
        "logsBloom": "0x" + "00" * 256,
        "logs": [
            {
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "topics": ["0x" + "cd" * 32],
                "data": "0x01",
                "logIndex": "0x0",
                "blockNumber": "0x7",
                "transactionHash": _HASH,
            }
        ],
    }
    return {**receipt, **changes}


def test_a_receipt_is_read_into_its_fields_with_addresses_and_hex_in_lowercase():
    rpc, _ = rpc_over(answering({"result": _receipt()}))
    assert rpc.receipt(_HASH.upper().replace("0X", "0x")) == Receipt(
        tx_hash=_HASH,
        block=7,
        succeeded=True,
        gas_used=16,
        effective_gas_price_wei=3,
        logs=(Log(_USDC.lower(), ("0x" + "cd" * 32,), "0x01"),),
    )
    assert rpc.receipt(_HASH).gas_cost_wei == 48


def test_a_receipt_the_node_does_not_have_is_none():
    rpc, _ = rpc_over(answering({"result": None}))
    assert rpc.receipt(_HASH) is None


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"transactionHash": "0x" + "ef" * 32}, "is of the transaction"),
        ({"status": "0x2"}, "has {"),
        # web3 hands a string of logs on character by character.
        ({"logs": "nope"}, "has a log of"),
    ],
)
def test_a_receipt_that_cannot_be_right_is_refused(changes, said):
    rpc, _ = rpc_over(answering({"result": _receipt(**changes)}))
    with pytest.raises(MalformedResponse, match=said):
        rpc.receipt(_HASH)


@pytest.mark.parametrize("logs", ["nope", None, 5])
def test_logs_that_are_not_a_list_are_refused_as_they_come(logs):
    # As another source than web3 would hand them on, unformatted.
    with pytest.raises(MalformedResponse, match="has logs of"):
        _parse_receipt(_HASH, {**_receipt(), "logs": logs, "blockNumber": 7, "status": 1,
                               "gasUsed": 16, "effectiveGasPrice": 3})


def test_the_node_reads_a_sender_needs_come_back_as_integers():
    rpc, _ = rpc_over(answering({"result": "0x2a"}))
    assert rpc.transaction_count("0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266") == 42
    assert rpc.max_priority_fee_wei() == 42
    assert rpc.estimate_gas({"to": _USDC, "data": "0x"}) == 42


def test_a_gas_estimate_of_nothing_is_refused():
    rpc, _ = rpc_over(answering({"result": "0x0"}))
    with pytest.raises(MalformedResponse, match="gas estimate"):
        rpc.estimate_gas({"to": _USDC, "data": "0x"})
