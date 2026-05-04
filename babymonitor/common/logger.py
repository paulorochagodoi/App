import logging
import logging.handlers
import os
import sys


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

        log_file = os.environ.get("LOG_FILE")
        if log_file:
            try:
                fh = logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                )
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            except OSError:
                pass  # fall back to stdout-only if file can't be opened

        logger.setLevel(level)
        logger.propagate = False
    return logger
