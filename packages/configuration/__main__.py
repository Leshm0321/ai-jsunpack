from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigurationError, load_application_config, redact_secrets


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m packages.configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "print-effective"):
        command = subparsers.add_parser(name)
        command.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        loaded = load_application_config(args.path)
    except ConfigurationError as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    if args.command == "validate":
        output = {
            "valid": True,
            "source": loaded.source,
            "fingerprint": loaded.fingerprint,
            "configFileConfigured": loaded.config_file is not None,
        }
    else:
        output = redact_secrets(loaded.config.model_dump(mode="json", by_alias=True))
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
