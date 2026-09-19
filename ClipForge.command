#!/bin/zsh
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"
.venv/bin/python clipforge.py
