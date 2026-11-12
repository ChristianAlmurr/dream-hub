#!/usr/bin/env python3
"""Removes the throwaway projects that test runs which are no longer running left behind, and says which.

Run: /usr/bin/python3 tests/sweep.py   (with the instance's six HUB_* variables set, as for the tests)
A throwaway project is named hub-test-throwaway-<n>, where <n> is the test run that made it. One whose run is still
going is left alone. For each of the others this removes its folder in ~/projects, its store in ~/agent-memory, what the
kit keeps for it in ~/.local/state/agent-memory, Claude Code's folders for it, and its sessions in this instance.
"""
from instance import leftovers, sweep

found = leftovers()
sweep()
print(f"{len(found)} throwaway {'project' if len(found) == 1 else 'projects'} removed." if found else "No throwaway project is left behind.")
