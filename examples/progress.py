#!/usr/bin/env python3
"""
A template for a tool that logs in colour and shows a progress bar.

The work is done by code that knows nothing of the terminal: it logs
through `logging` and reports progress through a callback. How that
looks is decided in one place, when the command line starts:

- on a terminal, rich draws the log lines and a progress bar,
- anywhere else (a pipe, a file, a test) the same run prints plain
  lines, progress included.

All of it goes to stderr, so that stdout stays free for results.

Besides fireaid this needs rich:

    pip install fireaid rich

Try:

    progress.py copy
    progress.py copy --size=3000000 --chunk=1024
    progress.py copy --debug
    progress.py levels
    progress.py copy 2>&1 | cat
    progress.py copy > result.txt
    NO_COLOR=1 progress.py copy
    progress.py help copy
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from time import sleep

import fireaid as fire

logger = logging.getLogger("progress")


# The work. It logs and calls back, and imports nothing for show.

# Called with the amount done so far and the total, in the work's own unit
Progress = Callable[[int, int], None]


def copy(size: int, chunk: int, progress: Progress | None = None) -> int:
    """Pretend to copy size bytes, chunk by chunk. Returns the number of chunks."""
    logger.info(f"Copying {size} bytes in chunks of {chunk}")
    chunks = 0
    for done in range(0, size, chunk):
        if progress:
            progress(done, size)
        logger.debug(f"Chunk at {done}")
        if chunks == size // chunk // 2:
            logger.warning("Half way, and the source is getting slow")
        sleep(0.002)
        chunks += 1
    if progress:
        progress(size, size)
    return chunks


# The presentation. rich stays in here, and is imported only when it is used.

_console = None  # the rich console of the running command, once logging is set up


def setup_logging(debug: bool = False) -> None:
    """Send log records to stderr, in the style that suits it."""
    global _console
    from rich.console import Console

    _console = Console(stderr=True)
    if _console.is_terminal:
        from rich.logging import RichHandler
        handler = RichHandler(console=_console, show_path=debug, omit_repeated_times=False, markup=False,
                              rich_tracebacks=True, log_time_format="%H:%M:%S")
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        handler = logging.StreamHandler()
        names = " %(name)s" if debug else ""
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s{names} %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[handler], force=True)


def log_progress(unit: str, steps: int = 8) -> Progress:
    """A progress callback that logs a line each time another 1/steps of the total is done."""
    last_step = -1

    def report(done: int, total: int) -> None:
        nonlocal last_step
        step = steps if done >= total else done * steps // total
        if step != last_step:
            last_step = step
            logger.info(f"{done} / {total} {unit} ({100 * step // steps}%)")

    return report


@contextmanager
def show_progress(description: str, unit: str):
    """The progress callback to hand to the work: a bar on a terminal, log lines otherwise."""
    if _console is None or not _console.is_terminal:
        yield log_progress(unit)
        return

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn, TextColumn, \
        TimeRemainingColumn

    columns = (TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(), MofNCompleteColumn(),
               TextColumn(unit), TimeRemainingColumn())
    # transient: the bar goes away when the work is done, and the log lines stay
    with Progress(*columns, console=_console, transient=True) as bar:
        task = bar.add_task(description, total=None)
        yield lambda done, total: bar.update(task, completed=done, total=total)


# The command line. fireaid makes it from the class, with help for every command.

class Tool:
    """Copy nothing, with a progress bar and log lines in colour.

    Log lines and the bar go to stderr, results to stdout.
    """

    def copy(self, size=20_000_000, chunk=2048, debug=False):
        """Copy, showing progress.

        Args:
            size: How many bytes to copy.
            chunk: How many bytes to copy at a time.
            debug: Log every chunk, and say where each line comes from.
        """
        setup_logging(debug)
        with show_progress("Copying", "bytes") as callback:
            chunks = copy(size, chunk, progress=callback)
        logger.info("Done")
        return f"{chunks} chunks"

    def levels(self, debug=False):
        """Log a line at every level, and an exception.

        Args:
            debug: Show the debug line too, and where each line comes from.
        """
        setup_logging(debug)
        logger.debug("Details for whoever is looking for a bug")
        logger.info("What the tool is doing")
        logger.warning("Something the user should know about")
        logger.error("Something that went wrong")
        logger.critical("Something that ends the run")
        try:
            int("forty-two")
        except ValueError:
            logger.exception("An exception, with its traceback")


if __name__ == "__main__":
    fire.Fire(Tool())
