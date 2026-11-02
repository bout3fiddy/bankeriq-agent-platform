"""Allow python -m bankeriq_agent_platform to use the same launch command."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
