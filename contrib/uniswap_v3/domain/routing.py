"""From a portfolio and its target weights to the swaps that close the gap.

Every swap is exact-input and sells a token the portfolio holds too much of
straight into one it holds too little of, along the pools that join the two.
A token is therefore either sold or bought in one rebalance, never both, and
no swap's input depends on another's output: the whole list is known before
the first swap is made.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from .decimal_context import DECIMAL_CONTEXT, EXACT_CONTEXT, floor_to_places
from .execution import ExecutionSettings
from .types import Pool, Portfolio, SwapIntent, TargetWeights, Token, tokens_along

__all__ = ["amount_out_at", "find_route", "plan_swaps"]


def find_route(pools: Sequence[Pool], token_in: Token, token_out: Token) -> tuple[Pool, ...]:
    """The fewest of ``pools``, in the order crossed, that lead from ``token_in`` to ``token_out``.

    Where the pools form a tree, as a config's must, there is one such path.
    """
    if token_in == token_out:
        raise ValueError(f"a route joins two different tokens, got {token_in.symbol} twice")
    routes: dict[Token, tuple[Pool, ...]] = {token_in: ()}
    frontier = [token_in]
    while frontier:
        reached: list[Token] = []
        for token in frontier:
            for pool in pools:
                if token not in (pool.token0, pool.token1):
                    continue
                other = pool.token1 if token == pool.token0 else pool.token0
                if other not in routes:
                    routes[other] = (*routes[token], pool)
                    reached.append(other)
        if token_out in routes:
            return routes[token_out]
        frontier = reached
    raise ValueError(f"no pool path joins {token_in.symbol} to {token_out.symbol}")


def amount_out_at(
    token_in: Token,
    route: Sequence[Pool],
    amount_in: Decimal,
    *,
    quote: str,
    prices: Mapping[str, Decimal],
) -> Decimal:
    """What ``amount_in`` would return along ``route`` at ``prices``, after the pools' fees.

    ``prices`` are quote-token prices, as a bar carries them, and ``quote``
    is the quote token's symbol. The amount is not cut to the output token's
    decimal places.
    """

    def price(token: Token) -> Decimal:
        return Decimal(1) if token.symbol == quote else prices[token.symbol]

    token_out = tokens_along(token_in, route)[-1]
    amount = DECIMAL_CONTEXT.divide(
        DECIMAL_CONTEXT.multiply(amount_in, price(token_in)), price(token_out)
    )
    for pool in route:
        kept = DECIMAL_CONTEXT.subtract(Decimal(1), pool.fee_rate)
        amount = DECIMAL_CONTEXT.multiply(amount, kept)
    return amount


def plan_swaps(
    portfolio: Portfolio,
    target: TargetWeights,
    *,
    tokens: Sequence[Token],
    pools: Sequence[Pool],
    settings: ExecutionSettings,
) -> tuple[SwapIntent, ...]:
    """The swaps that move ``portfolio`` to the ``target`` weights at its own prices.

    The token furthest above its target is matched with the one furthest
    below, then the next, until one side is used up; a tie goes to the
    symbol that sorts first. A transfer worth less than
    ``settings.min_trade_value`` is left out, and so is one too small to
    sell a single unit of its token or to return a single unit of the token
    it buys. Each swap's ``amount_in`` is
    cut to its token's decimal places, and its ``min_amount_out`` is what the
    route returns at the portfolio's prices after the pools' fees, less
    ``settings.max_slippage``.

    ``tokens`` are the configured tokens, which the portfolio and the target
    must both name exactly.
    """
    by_symbol = {token.symbol: token for token in tokens}
    if set(portfolio.balances) != set(by_symbol) or set(target.weights) != set(by_symbol):
        raise ValueError(
            f"the portfolio holds {sorted(portfolio.balances)} and the target names "
            f"{sorted(target.weights)}; both must be the tokens {sorted(by_symbol)}"
        )
    total = portfolio.total_value
    if total == 0:
        return ()

    def price(symbol: str) -> Decimal:
        return Decimal(1) if symbol == portfolio.quote else portfolio.prices[symbol]

    # Quote-token value still to move, per token over and per token under its target.
    excess: dict[str, Decimal] = {}
    deficit: dict[str, Decimal] = {}
    # What a token over its target may still sell, in its own units.
    sellable: dict[str, Decimal] = {}
    for symbol, token in by_symbol.items():
        wanted = DECIMAL_CONTEXT.multiply(target.weights[symbol], total)
        gap = DECIMAL_CONTEXT.subtract(wanted, portfolio.value_of(symbol))
        if gap > 0:
            deficit[symbol] = gap
        elif gap < 0:
            excess[symbol] = -gap
            kept = floor_to_places(DECIMAL_CONTEXT.divide(wanted, price(symbol)), token.decimals)
            # A target of zero keeps nothing, so the whole balance is sellable.
            surplus = EXACT_CONTEXT.subtract(portfolio.balances[symbol], kept)
            sellable[symbol] = floor_to_places(max(surplus, Decimal(0)), token.decimals)

    sellers = sorted(excess, key=lambda symbol: (-excess[symbol], symbol))
    buyers = sorted(deficit, key=lambda symbol: (-deficit[symbol], symbol))
    swaps: list[SwapIntent] = []
    while sellers and buyers:
        seller, buyer = sellers[0], buyers[0]
        value = min(excess[seller], deficit[buyer])
        if value == excess[seller]:
            # The seller's last transfer takes what is left, so no dust stays behind.
            amount = sellable[seller]
        else:
            amount = min(
                floor_to_places(
                    DECIMAL_CONTEXT.divide(value, price(seller)), by_symbol[seller].decimals
                ),
                sellable[seller],
            )
        sellable[seller] = EXACT_CONTEXT.subtract(sellable[seller], amount)
        if value >= settings.min_trade_value and amount > 0:
            route = find_route(pools, by_symbol[seller], by_symbol[buyer])
            at_close = amount_out_at(
                by_symbol[seller], route, amount, quote=portfolio.quote, prices=portfolio.prices
            )
            floor = floor_to_places(
                DECIMAL_CONTEXT.multiply(
                    at_close, DECIMAL_CONTEXT.subtract(Decimal(1), settings.max_slippage)
                ),
                by_symbol[buyer].decimals,
            )
            # A minimum of nothing would protect nothing, and no executor fills nothing.
            if floor > 0:
                swaps.append(
                    SwapIntent(
                        token_in=by_symbol[seller],
                        route=route,
                        amount_in=amount,
                        min_amount_out=floor,
                    )
                )
        excess[seller] = DECIMAL_CONTEXT.subtract(excess[seller], value)
        deficit[buyer] = DECIMAL_CONTEXT.subtract(deficit[buyer], value)
        if excess[seller] == 0:
            sellers.pop(0)
        if deficit[buyer] == 0:
            buyers.pop(0)
    return tuple(swaps)
