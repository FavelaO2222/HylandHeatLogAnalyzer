"""Create or migrate the research schema without importing logs or generating findings."""

import argparse
import sqlite3
import sys

from .db import SCHEMA_VERSION, initialize_database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', help='Database path (relative to the analyzer project; default: data/hylandheat.db)')
    args = parser.parse_args(argv)
    try:
        path = initialize_database(args.database)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    print(f'Initialized SQLite schema v{SCHEMA_VERSION}: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
