"""Command line: `python -m bdeploy check` (read-only) or `python -m bdeploy lifecycle` (writes)."""

from __future__ import annotations

import argparse
import os
import sys

from .client import BDeployClient, Config, ConfigurationError
from .scenarios import DEFAULT_TEST_URL, Reporter, generate_test_serial, run_live_check, run_lifecycle

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_CONFIGURATION_ERROR = 2


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bdeploy",
        description="B-Deploy v2/v3 test client. Credentials come from BS_CLIENT_ID, BS_SECRET and BS_NETWORK.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read-only live check: token, session network, list setups and devices")
    lifecycle = commands.add_parser(
        "lifecycle",
        help="live check, then create, read, update and delete a throwaway setup and device (needs BS_USERNAME)",
    )
    lifecycle.add_argument(
        "--serial",
        default=os.environ.get("BS_TEST_SERIAL"),
        help="12-character serial to register (default: BS_TEST_SERIAL, else a random ZZ-prefixed serial)",
    )
    lifecycle.add_argument(
        "--url",
        default=os.environ.get("BS_TEST_URL") or DEFAULT_TEST_URL,
        help="partner application URL for the test setup (default: BS_TEST_URL or a placeholder)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        config = Config.from_environment()
    except ConfigurationError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    client = BDeployClient(config)
    reporter = Reporter()
    try:
        run_live_check(client, reporter)
        if arguments.command == "lifecycle":
            run_lifecycle(client, reporter, arguments.serial or generate_test_serial(), arguments.url)
    except Exception as error:
        print(f"\nstopped: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED

    passed = sum(1 for result in reporter.results if result.passed)
    print(f"\n{passed}/{len(reporter.results)} steps passed")
    return EXIT_PASSED if reporter.all_passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
