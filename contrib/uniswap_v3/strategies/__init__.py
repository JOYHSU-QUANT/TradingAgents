"""Strategies: each answers "what should the weights be" through :class:`~..ports.Strategy`.

:mod:`.registry` maps the name a config uses to the factory that builds the
strategy. Three ship here: :mod:`.fixed_weights`, a placeholder that drives
the engine and its tests; :mod:`.trend_vol_weights`, a rule strategy that
holds a token while it trends, sized to a volatility target; and
:mod:`.ai_gated_weights`, which holds the rule's weights cut by what an
outside judge said of each token, read from the view's verdicts.
"""
