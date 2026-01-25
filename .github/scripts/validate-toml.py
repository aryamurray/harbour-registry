#!/usr/bin/env python3
"""
Validate shim TOML files for syntax and policy compliance.

Checks:
1. Valid TOML syntax
2. Required fields present (package.name, package.version, source)
3. Platform count meets minimum requirement (from config.toml)
4. Git source has 40-char commit SHA
5. Version string is valid semver
"""

import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


class ValidationError:
    def __init__(self, file: str, message: str, level: str = "error"):
        self.file = file
        self.message = message
        self.level = level  # "error" or "warning"

    def to_github_annotation(self) -> str:
        return f"::{self.level} file={self.file}::{self.message}"


def load_registry_config() -> dict:
    """Load registry config.toml for validation rules."""
    config_path = Path("config.toml")
    if not config_path.exists():
        return {}

    with open(config_path, "rb") as f:
        return tomllib.load(f)


def validate_semver(version: str) -> bool:
    """Check if version is valid semver."""
    # Basic semver pattern (simplified)
    pattern = r"^\d+\.\d+\.\d+(-[\w.]+)?(\+[\w.]+)?$"
    return bool(re.match(pattern, version))


def validate_git_sha(sha: str) -> bool:
    """Check if SHA is a 40-character hex string."""
    return bool(re.match(r"^[0-9a-fA-F]{40}$", sha))


def validate_shim(shim_path: str, config: dict) -> list[ValidationError]:
    """Validate a single shim file."""
    errors = []

    # Parse TOML
    try:
        with open(shim_path, "rb") as f:
            shim = tomllib.load(f)
    except Exception as e:
        errors.append(ValidationError(shim_path, f"Invalid TOML syntax: {e}"))
        return errors  # Can't continue if parsing failed

    # Check required package fields
    package = shim.get("package", {})
    if not package.get("name"):
        errors.append(ValidationError(shim_path, "Missing required field: package.name"))
    if not package.get("version"):
        errors.append(ValidationError(shim_path, "Missing required field: package.version"))
    elif not validate_semver(package["version"]):
        errors.append(ValidationError(shim_path, f"Invalid semver version: {package['version']}"))

    # Check source
    source = shim.get("source", {})
    if not source.get("git") and not source.get("tarball"):
        errors.append(ValidationError(shim_path, "Missing required field: source.git or source.tarball"))

    # Validate git source
    if git_source := source.get("git"):
        if not git_source.get("url"):
            errors.append(ValidationError(shim_path, "Git source missing 'url'"))
        if not git_source.get("rev"):
            errors.append(ValidationError(shim_path, "Git source missing 'rev' (commit SHA)"))
        elif not validate_git_sha(git_source["rev"]):
            errors.append(ValidationError(
                shim_path,
                f"Git 'rev' must be a 40-character commit SHA, got: {git_source['rev']}"
            ))

    # Validate tarball source
    if tarball_source := source.get("tarball"):
        if not tarball_source.get("url"):
            errors.append(ValidationError(shim_path, "Tarball source missing 'url'"))
        if not tarball_source.get("sha256"):
            errors.append(ValidationError(shim_path, "Tarball source missing 'sha256' checksum"))

    # Check platform requirements (from registry config)
    curated = config.get("curated", {})
    min_platforms = curated.get("min_platform_count", 3)

    metadata = shim.get("metadata", {})
    platforms = metadata.get("platforms", {})
    enabled_platforms = [p for p, enabled in platforms.items() if enabled]

    if len(enabled_platforms) < min_platforms:
        errors.append(ValidationError(
            shim_path,
            f"Shim declares {len(enabled_platforms)} platforms, minimum required is {min_platforms}. "
            f"Enabled: {enabled_platforms}"
        ))

    # Check for harness if required
    if curated.get("requires_harness", False):
        harness = metadata.get("harness")
        if not harness:
            errors.append(ValidationError(
                shim_path,
                "Registry requires harness test but shim has no [metadata.harness] section",
                level="warning"
            ))
        elif not harness.get("header") or not harness.get("test_call"):
            errors.append(ValidationError(
                shim_path,
                "Harness config missing required fields: header, test_call",
                level="warning"
            ))

    return errors


def main():
    # Get changed shims from environment
    changed_shims_json = os.environ.get("CHANGED_SHIMS", "[]")

    try:
        changed_shims = json.loads(changed_shims_json)
    except json.JSONDecodeError:
        # If not provided, validate all shims in index/
        import glob
        changed_shims = glob.glob("index/**/*.toml", recursive=True)

    if not changed_shims:
        print("No shims to validate")
        return 0

    # Load registry config
    config = load_registry_config()

    # Validate each shim
    all_errors = []
    for shim_path in changed_shims:
        errors = validate_shim(shim_path, config)
        all_errors.extend(errors)

    # Output GitHub annotations
    has_errors = False
    for error in all_errors:
        print(error.to_github_annotation())
        if error.level == "error":
            has_errors = True

    # Summary
    error_count = sum(1 for e in all_errors if e.level == "error")
    warning_count = sum(1 for e in all_errors if e.level == "warning")

    print(f"\nValidation complete: {len(changed_shims)} shims, {error_count} errors, {warning_count} warnings")

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
