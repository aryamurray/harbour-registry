#!/usr/bin/env python3
"""
Detect which shim files have changed in a PR or push.

Outputs:
  - GITHUB_OUTPUT: has_changes=true/false, changed_shims=JSON list of paths
"""

import json
import os
import subprocess
import sys


def get_changed_files() -> list[str]:
    """Get list of changed files between base and head commits."""
    # For pull requests, compare against the base branch
    base_sha = os.environ.get("GITHUB_BASE_REF")
    head_sha = os.environ.get("GITHUB_SHA", "HEAD")

    if base_sha:
        # Pull request: compare against base branch
        base_ref = f"origin/{base_sha}"
    else:
        # Push: compare against parent commit
        before_sha = os.environ.get("GITHUB_EVENT_BEFORE")
        if before_sha and before_sha != "0" * 40:
            base_ref = before_sha
        else:
            # Fallback: compare against HEAD~1
            base_ref = "HEAD~1"

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, head_sha],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip().split("\n") if result.stdout.strip() else []
    except subprocess.CalledProcessError:
        # Fallback: get all files in index/
        result = subprocess.run(
            ["git", "ls-files", "index/"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip().split("\n") if result.stdout.strip() else []


def filter_shim_files(files: list[str]) -> list[str]:
    """Filter to only shim TOML files in index/."""
    return [
        f for f in files
        if f.startswith("index/") and f.endswith(".toml")
    ]


def main():
    # Check for manual package input
    manual_package = os.environ.get("INPUT_PACKAGE", "").strip()

    if manual_package:
        # Manual run with specific package - find all versions
        import glob
        first_char = manual_package[0].lower()
        pattern = f"index/{first_char}/{manual_package}/*.toml"
        shim_files = glob.glob(pattern)
    else:
        # Detect from git diff
        changed = get_changed_files()
        shim_files = filter_shim_files(changed)

    has_changes = len(shim_files) > 0

    # Write outputs
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"has_changes={'true' if has_changes else 'false'}\n")
            f.write(f"changed_shims={json.dumps(shim_files)}\n")

    # Also print for debugging
    print(f"Changed shims: {shim_files}")
    print(f"Has changes: {has_changes}")

    return 0 if has_changes else 0  # Always succeed


if __name__ == "__main__":
    sys.exit(main())
