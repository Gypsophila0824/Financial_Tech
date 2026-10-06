from __future__ import annotations

import argparse

from .validation import load_and_validate


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Taiwan equity agent data contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--table", choices=("security_master", "universe_audit"), required=True)
    validate.add_argument("--input", required=True)
    validate.add_argument("--profile", choices=("example", "production"), default="example")
    args = parser.parse_args()
    issues = load_and_validate(args.input, args.table, args.profile)
    for issue in issues:
        print(f"{issue.severity.upper()} [{issue.code}] {issue.message}")
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
