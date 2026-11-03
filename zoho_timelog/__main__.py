"""The `timelog` command (and `python -m zoho_timelog`).

`timelog hook` runs before every Claude Code prompt, so it's dispatched before cli.py, and with it
requests, starlette and the Zoho code, is imported.
"""

import sys


def main() -> None:
    if sys.argv[1:2] == ["hook"]:
        from .hook import main as hook_main
        sys.exit(hook_main())
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
