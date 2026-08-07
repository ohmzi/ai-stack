#!/usr/bin/env python3
"""Harness for scripts/flight_probe.py — the site-measuring instrument.

DELEGATES to that script's own --selftest rather than restating its checks. Being honest about what
this file is: it adds no coverage of its own. It exists because the coverage lived ONLY in
`python3 scripts/flight_probe.py --selftest`, which is invisible to anyone running tests/ and
inconsistent with the other 38 suites here. Duplicating ~60 assertions into a second file would give
two places to update and one of them would rot.

The checks themselves are colocated with the code deliberately: flight_probe's verdict logic is a pure
function of a recorded measurement, so its fixtures are inline literals rather than files, and
keeping them next to the rules they pin is what makes a rule change and its test a single edit.

Usage:  python3 tests/test_flight_probe.py
"""
import importlib.util
import os
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "flight_probe.py")
spec = importlib.util.spec_from_file_location("flight_probe", os.path.normpath(SCRIPT))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

sys.exit(mod.selftest())
