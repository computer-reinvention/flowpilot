"""Local ./chat REPL entry (backend.repl main) with the deterministic model boundary.

`./chat` is `uv run python -m backend.repl "$@"`. This launcher runs that same
`main()` with the same argv, replacing only `pi_browser.run_pi` (the model) and
suppressing dotenv discovery so no checkout-level provider credentials load.
The REPL itself, BrowserSession, its Docker sandbox, scripts and memory are real.
"""

import asyncio
import sys


def main():
    import dotenv
    from backend import pi_browser
    from .assembled_run import deterministic_driver
    dotenv.load_dotenv = lambda *args, **kwargs: False  # Never read a parent .env.
    pi_browser.run_pi = deterministic_driver
    from backend.repl import main as repl_main
    sys.argv = ['backend.repl', *sys.argv[1:]]
    asyncio.run(repl_main())


if __name__ == '__main__':
    main()
