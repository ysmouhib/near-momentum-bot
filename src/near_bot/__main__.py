"""Allow ``python -m near_bot <command>`` in addition to the ``near-bot`` script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
