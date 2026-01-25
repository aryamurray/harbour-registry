#!/usr/bin/env python3
"""
Generate GitHub Actions build matrix from shim platform declarations.

Reads the changed shims and generates a matrix that ONLY includes
platforms declared as `true` in each shim's [metadata.platforms] section.

Example: If zlib/1.3.1.toml has:
    [metadata.platforms]
    linux = true
    macos = true
    windows = true
    freebsd = true

Then the matrix generates 4 jobs for zlib.

If niche-lib/1.0.0.toml only has:
    [metadata.platforms]
    linux = true

Then only 1 job runs for niche-lib.
"""

import json
import os
import sys

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


# Platform to runner and build configuration mapping
PLATFORM_RUNNERS = {
    "linux": {
        "runner": "ubuntu-latest",
        "target_triple": None,
        "skip_harness": False,
    },
    "macos": {
        "runner": "macos-latest",
        "target_triple": None,
        "skip_harness": False,
    },
    "windows": {
        "runner": "windows-latest",
        "target_triple": None,
        "skip_harness": False,
    },
    "freebsd": {
        "runner": "ubuntu-latest",
        "target_triple": "x86_64-unknown-freebsd",
        "skip_harness": True,  # Cross-compile only, no harness
    },
    "android": {
        "runner": "ubuntu-latest",
        "target_triple": "aarch64-linux-android",
        "skip_harness": True,  # Cross-compile only
    },
    "ios": {
        "runner": "macos-latest",
        "target_triple": "aarch64-apple-ios",
        "skip_harness": True,  # Cross-compile only
    },
}


def parse_shim(shim_path: str) -> dict | None:
    """Parse a shim TOML file and extract relevant info."""
    try:
        with open(shim_path, "rb") as f:
            shim = tomllib.load(f)
        return shim
    except Exception as e:
        print(f"::error file={shim_path}::Failed to parse shim: {e}", file=sys.stderr)
        return None


def get_declared_platforms(shim: dict) -> dict[str, bool]:
    """Extract platform declarations from shim metadata."""
    metadata = shim.get("metadata", {})
    platforms = metadata.get("platforms", {})
    return platforms


def generate_matrix(changed_shims: list[str]) -> dict:
    """Generate build matrix from shim platform declarations."""
    include = []

    for shim_path in changed_shims:
        shim = parse_shim(shim_path)
        if shim is None:
            continue

        pkg_name = shim.get("package", {}).get("name", "unknown")
        pkg_version = shim.get("package", {}).get("version", "unknown")
        platforms = get_declared_platforms(shim)

        if not platforms:
            print(f"::warning file={shim_path}::Shim has no platform declarations", file=sys.stderr)
            continue

        # ONLY add matrix entries for platforms declared as true
        for platform, enabled in platforms.items():
            if not enabled:
                continue

            if platform not in PLATFORM_RUNNERS:
                print(f"::warning file={shim_path}::Unknown platform '{platform}'", file=sys.stderr)
                continue

            runner_config = PLATFORM_RUNNERS[platform]
            entry = {
                "package": pkg_name,
                "version": pkg_version,
                "platform": platform,
                "shim_path": shim_path,
                "runner": runner_config["runner"],
                "target_triple": runner_config["target_triple"] or "",
                "skip_harness": runner_config["skip_harness"],
            }
            include.append(entry)

    return {"include": include}


def main():
    # Get changed shims from environment (set by detect-changed-shims.py)
    changed_shims_json = os.environ.get("CHANGED_SHIMS", "[]")

    # Also check for GITHUB_OUTPUT from previous step
    if changed_shims_json == "[]":
        # Try reading from previous step output file
        output_file = os.environ.get("GITHUB_OUTPUT")
        # This is a simplified version - in practice the workflow passes the output

    try:
        changed_shims = json.loads(changed_shims_json)
    except json.JSONDecodeError:
        print(f"::error::Invalid CHANGED_SHIMS JSON: {changed_shims_json}", file=sys.stderr)
        changed_shims = []

    # Generate matrix
    matrix = generate_matrix(changed_shims)

    # Write output
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            # GitHub Actions needs the JSON on a single line
            f.write(f"matrix={json.dumps(matrix)}\n")

    # Also print for debugging
    print(f"Generated matrix with {len(matrix['include'])} jobs:")
    for entry in matrix["include"]:
        print(f"  - {entry['package']}@{entry['version']} / {entry['platform']} on {entry['runner']}")

    # Output the matrix JSON
    print(json.dumps(matrix, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
