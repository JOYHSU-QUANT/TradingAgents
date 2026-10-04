"""The engine: one step per bar, the same in every run mode.

:mod:`.step` holds the step and what starts and reopens a run;
:mod:`.executors` holds the executors that need no chain; :mod:`.backtest`
replays stored bars through the step. A mode differs only in the
:class:`~..ports.Executor` and the bars it hands the step.
"""
