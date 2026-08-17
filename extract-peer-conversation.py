#!/usr/bin/env python3
"""Thin wrapper so a git clone keeps working. The implementation is
chatter/mdlog.py, which is where it has to live for the installed entry point
to import it — a console script cannot subprocess a sibling file that pip
never copied."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chatter.mdlog import main  # noqa: E402

if __name__ == "__main__":
    main()
