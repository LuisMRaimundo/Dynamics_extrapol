"""Backward-compatible shim — prefer `import dynamics_predicter` or `python -m dynamics_predicter`."""

from dynamics_predicter.transfer import *  # noqa: F403
from dynamics_predicter.transfer import __version__, main

if __name__ == "__main__":
    raise SystemExit(main())
