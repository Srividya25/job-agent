"""Registry of ATS adapters.

Adding a source is: write the module, import it, add it to REGISTRY.
Nothing else in the codebase changes.
"""

from __future__ import annotations

from types import ModuleType

from ...models import ATS
from . import ashby, greenhouse, lever, workday

REGISTRY: dict[ATS, ModuleType] = {
    ATS.GREENHOUSE: greenhouse,
    ATS.LEVER: lever,
    ATS.ASHBY: ashby,
    ATS.WORKDAY: workday,
}

# Probe order matters: cheapest and most common first, so `detect_ats` stops
# early for the majority of companies. Workday is last because it only ever
# answers for tenants already in its registry — there is nothing to discover.
PROBE_ORDER: list[ATS] = [ATS.GREENHOUSE, ATS.LEVER, ATS.ASHBY, ATS.WORKDAY]

__all__ = ["REGISTRY", "PROBE_ORDER", "greenhouse", "lever", "ashby", "workday"]
