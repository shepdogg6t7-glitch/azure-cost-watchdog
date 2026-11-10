"""costwatchdog - find idle and orphaned Azure resources, reclaim the spend, stay safe.

The package is split so that the decision logic is pure and testable:

* :mod:`costwatchdog.models` - plain data structures (no Azure, no I/O).
* :mod:`costwatchdog.billing` - parses Azure Cost Management exports into per-resource
  monthly run rates.
* :mod:`costwatchdog.detectors` - decides whether a resource looks idle or orphaned.
* :mod:`costwatchdog.policy` - the guardrail configuration.
* :mod:`costwatchdog.engine` - combines the above into ranked, guardrailed decisions.
* :mod:`costwatchdog.report` - renders Markdown/JSON reports.
"""

from costwatchdog.engine import evaluate
from costwatchdog.policy import Policy, PolicyError

__all__ = ["evaluate", "Policy", "PolicyError"]
__version__ = "0.1.0"
