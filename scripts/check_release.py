"""Reject tags that do not match the installed distribution version."""

from importlib.metadata import version
import sys


def check_tag(tag: str, package_version: str) -> None:
    expected = f"v{package_version}"
    if tag != expected:
        raise ValueError(f"Tag {tag!r} does not match package version {package_version!r}; expected {expected!r}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_release.py vX.Y.Z")
    try:
        check_tag(sys.argv[1], version("claudex-export"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Verified release tag {sys.argv[1]}")
