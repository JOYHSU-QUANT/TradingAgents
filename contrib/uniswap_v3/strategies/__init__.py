"""Strategies: each answers "what should the weights be" through :class:`~..ports.Strategy`.

:mod:`.registry` maps the name a config uses to the factory that builds the
strategy. Four ship here: :mod:`.fixed_weights`, a placeholder that drives
the engine and its tests; :mod:`.trend_vol_weights`, a rule strategy that
holds a token while it trends, sized to a volatility target;
:mod:`.ai_gated_weights`, which holds the rule's weights cut by what an
outside judge said of each token, read from the view's verdicts; and
:mod:`.external_weights`, which holds what an outside coordinator set,
read from the view's targets, and holds still when it set nothing.
"""
