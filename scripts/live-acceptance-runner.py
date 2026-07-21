#!/usr/bin/env python3
"""Compatibility CLI for the package-owned live acceptance runner."""

from acceptance.runner import main


if __name__ == "__main__":
    raise SystemExit(main())
