#!/usr/bin/env python3
"""Backward-compatible entrypoint for the governed Docs API change workflow."""

from run_api_change_workflow import main


if __name__ == "__main__":
    raise SystemExit(main())
