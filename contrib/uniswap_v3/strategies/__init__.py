"""Strategies: each answers "what should the weights be" through :class:`~..ports.Strategy`.

:mod:`.registry` maps the name a config uses to the factory that builds the
strategy. Two ship here: :mod:`.fixed_weights`, a placeholder that drives
the engine and its tests, and :mod:`.trend_vol_weights`, a rule strategy
that holds a token while it trends, sized to a volatility target.
"""
