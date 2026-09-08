"""``tradingagents.node_names``: one spelling of a node name across layers."""

import ast

import pytest

from tradingagents.node_names import PORTFOLIO_MANAGER_NODE

from .conftest import repo_text


@pytest.mark.unit
def test_the_cli_spells_the_portfolio_manager_node_from_the_constant():
    # The CLI's agent_status keys, its report-section map and the decision
    # panel title are the node's name; spelled from the constant, a rename
    # there cannot leave the CLI on yesterday's string (#214). Walk the AST
    # rather than grep a quoting style: any string constant that IS the name
    # — single- or double-quoted, dict key or call argument — is a regression.
    # The prose sites (a section header, a markdown heading) embed the name in
    # a longer sentence and are display text, not keys.
    bare = [
        node.lineno
        for node in ast.walk(ast.parse(repo_text("cli/main.py")))
        if isinstance(node, ast.Constant) and node.value == PORTFOLIO_MANAGER_NODE
    ]
    assert not bare, f"cli/main.py spells the node name by hand at lines {bare}"
    # And the two class-level tables resolve to the constant at runtime.
    # (Importing cli.main is ~10 s cold; amortised across the CLI suite.)
    from cli.main import MessageBuffer

    assert MessageBuffer.REPORT_SECTIONS["final_trade_decision"] == (None, PORTFOLIO_MANAGER_NODE)
    assert MessageBuffer.FIXED_AGENTS["Portfolio Management"] == [PORTFOLIO_MANAGER_NODE]
