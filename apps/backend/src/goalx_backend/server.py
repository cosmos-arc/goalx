"""
Fork-safe GoalX API server entrypoint.

Delegates to the Granian CLI; arguments after the module path are forwarded,
e.g. `python -m goalx_backend.server goalx_backend.main:app --interface asgi`.
"""

from __future__ import annotations

from granian.cli import entrypoint as granian_entrypoint


def main() -> None:
    """Hand control to Granian's CLI entrypoint."""
    granian_entrypoint()


if __name__ == "__main__":  # pragma: no cover - exercised by process smoke tests
    main()
