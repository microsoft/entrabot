"""Allow `python -m entrabot.harness …` as an alternative to the `entrabot-harness` script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
