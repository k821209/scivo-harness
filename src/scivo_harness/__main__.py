"""`python -m scivo_harness` — the same entry point as the `scivo` script.

/update re-executes scivo through the interpreter this way, so it does not
depend on where the console script landed on PATH.
"""
from .cli import main

raise SystemExit(main())
