"""Root conftest — ensures the workspace root is on sys.path so `src` is importable."""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
