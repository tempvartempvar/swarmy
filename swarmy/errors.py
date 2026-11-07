"""Which exceptions end a whole run, and which are contained where they happen."""

from __future__ import annotations

from .budget import RunBudgetExceeded
from .providers import FatalProviderError

STOPS_RUN = (FatalProviderError, RunBudgetExceeded)


def stops_run(exc: BaseException) -> bool:
    """True for errors that must end the run: the endpoint refused it (bad key, no credits, unknown
    model) or the budget ran out, also when they arrive inside an ExceptionGroup from a TaskGroup.
    Anything else (a bug, a bad file, an odd response) is contained and reported where it happened."""
    if isinstance(exc, STOPS_RUN):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(lambda e: isinstance(e, STOPS_RUN)) is not None
    return False
