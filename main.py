"""Entrypoint: load config.yaml and run the bridge's HTTP server with uvicorn.

Run natively, no containerization:

    python main.py [path/to/config.yaml]
"""

from __future__ import annotations

import logging
import re
import sys

import uvicorn

from bridge.config import ConfigError, load_config
from bridge.server import create_app

_NOTE_OBJECT_404_RE = re.compile(r"^/actor/[^/]+/notes/[^/]+$")


class _ExpectedNotFoundFilter(logging.Filter):
    """Suppress uvicorn's access-log line for expected, high-volume 404s.

    ``/media/...`` -- remote fediverse servers that cached references to
    media from a previous, non-bridge instance of this domain (e.g. an
    earlier Pleroma install) keep retrying those now-defunct URLs
    indefinitely; the media proxy 404s all of them by design (see its
    published-media allowlist).

    ``/actor/{username}/notes/{id}`` -- remote servers dereferencing a Note's
    own AP id directly (e.g. to verify/render a quote-post or repost, or a
    repost fetching the object because it was sent as a bare IRI rather than
    embedded -- see ``bridge.activitypub.routes.get_note``, which now
    actually serves these). A genuine 404 here still means a stale/bad
    reference (a deleted post, wrong note id, ...) rather than the route
    itself being unimplemented, and remote servers retry those just as
    persistently as the media case, so it's still not worth logging at the
    volume it occurs.

    Neither is actionable in the log at the volume they occur, so both are
    suppressed here; everything else still logs normally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs this as `logger.info('%s - "%s %s HTTP/%s" %d', client_addr,
        # method, path, http_version, status_code)` -- read the structured args
        # directly rather than pattern-matching the formatted text.
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status_code = args[2], args[4]
        if status_code != 404 or not isinstance(path, str):
            return True
        return not (path.startswith("/media/") or _NOTE_OBJECT_404_RE.match(path))


def main(argv: list[str]) -> int:
    config_path = argv[0] if argv else None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_ExpectedNotFoundFilter())

    app = create_app(config)
    # uvicorn's own logging.config.dictConfig hardcodes "uvicorn"/
    # "uvicorn.error"/"uvicorn.access" to level INFO with propagate=False,
    # regardless of what's configured above -- log_config=None disables that
    # setup entirely, so those loggers fall back to propagating to (and
    # respecting the level of) the root logger like everything else instead
    # of always logging every request at INFO no matter what.
    uvicorn.run(app, host=config.bridge.listen_host, port=config.bridge.listen_port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
