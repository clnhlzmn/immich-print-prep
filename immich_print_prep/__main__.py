"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="immich-print-prep",
        description="Web UI for preparing Immich photos for print ordering.",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--host", default=os.environ.get("IPP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IPP_PORT", "8000")))
    parser.add_argument(
        "--log-level", default=os.environ.get("IPP_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug"],
    )
    parser.add_argument("--check-config", action="store_true", help="validate the config and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2

    if args.check_config:
        print(
            "ok: immich_url=%s users=%s data_dir=%s"
            % (config.immich_url, ",".join(u.username for u in config.users), config.data_dir)
        )
        return 0

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(config),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=args.log_level == "debug",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
