"""Make the top-level modules (app, collectors, launcher, store) importable from
tests regardless of pytest's import mode."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
