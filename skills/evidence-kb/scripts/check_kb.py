#!/usr/bin/env python3
"""Deprecated name of kb.py, kept so that CI scripts written for 1.0.0 keep
working. It runs kb.py with the same arguments; use kb.py instead."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
