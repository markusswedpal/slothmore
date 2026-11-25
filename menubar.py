#!/usr/bin/env python3
"""Kept for compatibility: the menu bar helper now lives in macos/menubar.py."""
import os
import runpy
import sys

sys.argv[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macos", "menubar.py")
runpy.run_path(sys.argv[0], run_name="__main__")
