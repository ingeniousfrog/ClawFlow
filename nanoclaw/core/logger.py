"""Structured logging for nanoClaw."""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

# Global verbose flag - also check env var at startup
_verbose = os.environ.get("NANOCLAW_VERBOSE", "").lower() in ("1", "true", "yes")


def set_verbose(verbose: bool) -> None:
    """Enable or disable verbose (DEBUG) logging for all nanoclaw loggers."""
    global _verbose
    _verbose = verbose
    level = logging.DEBUG if verbose else logging.INFO

    # Update root nanoclaw logger and its handlers
    root = logging.getLogger("nanoclaw")
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)

    # Also update root logger if no handlers on nanoclaw
    if not root.handlers:
        # Ensure nanoclaw logger has a handler
        setup_logger("nanoclaw", level)

    # Update all existing child loggers
    for name in list(logging.Logger.manager.loggerDict.keys()):
        if name.startswith("nanoclaw"):
            child = logging.getLogger(name)
            child.setLevel(level)
            for handler in child.handlers:
                handler.setLevel(level)


def setup_logger(
    name: str = "nanoclaw",
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """
    Set up a logger with console and optional file output.

    Args:
        name: Logger name
        level: Logging level
        log_file: Optional file path for logging

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Use verbose level if set globally
    if _verbose:
        level = logging.DEBUG

    logger.setLevel(level)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    # Format: timestamp - level - message
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# Global logger instance
logger = setup_logger()


def get_logger(name: str = "nanoclaw") -> logging.Logger:
    """Get a logger instance."""
    child = logging.getLogger(name)
    # Apply verbose level if set globally
    if _verbose:
        child.setLevel(logging.DEBUG)
    return child
