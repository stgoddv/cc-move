"""Time-based progress reporting.

At most one line per interval (60 s by default): a small repo finishes before the first line and prints
nothing, a large one shows it is alive about once a minute. Printing by percentage instead would flood
small repos with lines, which also costs tokens when an agent reads the output.
"""

import datetime as dt
import time

from . import log

DEFAULT_INTERVAL = 60


class Throttle:
    """Says when at least `interval` seconds have passed since the last time it said so."""

    def __init__(self, interval, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.last = clock()

    def ready(self):
        now = self.clock()
        if now - self.last >= self.interval:
            self.last = now
            return True
        return False


class Heartbeat:
    """For stages with an unknown total (scans): '<label>: N files so far', at most once per interval."""

    def __init__(self, label, interval=DEFAULT_INTERVAL, clock=time.monotonic):
        self.label = label
        self.throttle = Throttle(interval, clock)

    def __call__(self, files_so_far):
        if self.throttle.ready():
            log(f"  {self.label}: {files_so_far:,} files so far")


class Progress:
    """Overall progress of the heavy stages (hash source, copy, hash destination), measured in bytes.

    Each line is also written to `progress_file` (overwritten), so the user can look at it at any time
    and an agent can read just that line when asked, instead of polling the command's output.
    """

    def __init__(self, total_bytes, interval=DEFAULT_INTERVAL, progress_file=None, clock=time.monotonic):
        self.total = max(total_bytes, 1)
        self.progress_file = progress_file
        self.clock = clock
        self.started = clock()
        self.throttle = Throttle(interval, clock)
        self.done = 0
        self.stage = ""
        self.stage_done = 0
        self.stage_total = 0

    def start_stage(self, name, total_bytes):
        """Begins a stage; the line shows how far this stage is, the percentage is overall."""
        self.stage, self.stage_done, self.stage_total = name, 0, total_bytes

    def advance(self, n_bytes):
        """Counts bytes processed and prints a line if the interval has passed."""
        self.done += n_bytes
        self.stage_done += n_bytes
        self.heartbeat()

    def heartbeat(self):
        """Prints a line if the interval has passed, even if no bytes were counted (e.g. a huge file)."""
        if self.throttle.ready():
            self._report(self.line())

    def line(self):
        """'  37% | copying 12.1 GB of 48.2 GB | 9 min elapsed, ~15 min left'."""
        elapsed = self.clock() - self.started
        done = min(self.done, self.total)
        text = (f"  {100 * done / self.total:.0f}% | {self.stage} {_size(self.stage_done)} of "
                f"{_size(self.stage_total)} | {_duration(elapsed)} elapsed")
        if done:
            left = _duration(elapsed * (self.total - done) / done)
            text += f", {'' if left.startswith('<') else '~'}{left} left"
        return text

    def finish(self, status):
        """Leaves the final status in the progress file (the console already shows it)."""
        self._write(f"  {status} after {_duration(self.clock() - self.started)}")

    def _report(self, text):
        """Prints a progress line and keeps it in the progress file."""
        log(text)
        self._write(text)

    def _write(self, text):
        """Overwrites the progress file with one timestamped line."""
        if self.progress_file:
            with open(self.progress_file, "w", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%H:%M:%S}{text}\n")


def _size(n_bytes):
    """Human-readable size: MB below 1 GB, GB above."""
    return f"{n_bytes / 1e9:.1f} GB" if n_bytes >= 1e9 else f"{n_bytes / 1e6:.0f} MB"


def _duration(seconds):
    """'<1 min', '9 min' or '1 h 05 min'."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1 min"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"
