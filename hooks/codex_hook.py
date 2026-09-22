#!/usr/bin/env python3
"""Kept so the Codex hooks.json you already approved keeps working: the adapter now lives in agent_hook.py."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_hook  # noqa: E402

if __name__ == '__main__':
    agent_hook.run('codex')
