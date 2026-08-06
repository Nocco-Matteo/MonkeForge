"""The generalized "eyes" runner subpackage (TASK-012).

Re-exports the public surface used by ``quality_gates.ux_render`` and
``run.py::_run_eyes``:

- ``run_eyes`` — Playwright browser lifecycle + trace execution + facts/PNGs.
- ``validate_trace`` — closed action allowlist validation (pure, no I/O).
- ``discover_screens`` — bounded same-origin link crawl.
- ``normalize_legacy_facts`` — legacy subprocess facts → v1 ingest map.
- ``EyesError`` — clear error for deferred Electron / infra failures.
"""
from .legacy_ingest import normalize_legacy_facts
from .runner import EyesError, run_eyes
from .trace_validator import validate_trace
from .discovery import discover_screens

__all__ = [
    "run_eyes",
    "validate_trace",
    "discover_screens",
    "normalize_legacy_facts",
    "EyesError",
]
