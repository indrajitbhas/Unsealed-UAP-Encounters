"""PURSUE R01 Extraction Probe — Phase 0 validation workflow.

A human-in-the-loop pipeline that processes Release 01 of the PURSUE UAP
portal (https://www.war.gov/ufo/) to size a full six-tranche extraction
project while keeping Azure spend under a hard cap.

The package is intentionally modular: each of the five workflow steps lives
in its own module and is orchestrated by ``run_agent.py`` in the repo root.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
