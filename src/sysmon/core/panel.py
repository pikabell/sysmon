"""Base class for sysmon panels.

Every panel owns a polling worker for exactly one subsystem. The worker keeps all
blocking I/O (``/proc`` reads, NVML calls, ``nvidia-smi``) on a worker thread via
``asyncio.to_thread``, so a slow sample can never stall the Textual event loop or
freeze the display.

Panels only run while visible: :meth:`activate` starts the worker and
:meth:`deactivate` cancels it, so a hidden tab costs nothing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker
from textual.binding import Binding
from textual.containers import VerticalScroll

from .context import SOURCES
from ..config import Settings


class Panel(Widget):
    """A full-screen view backed by a periodically sampled data source."""

    DEFAULT_CSS = """
    Panel {
        height: 1fr;
        width: 100%;
        layout: vertical;
    }
    /* One-line status strip pinned to the bottom of every panel. */
    Panel > .statusline {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $panel;
    }
    Panel > .statusline.error {
        color: $error;
        background: $boost;
    }
    """

    #: Panels without a table need to be focusable so they can receive arrow keys
    #: and scroll their content. Tables are focusable in their own right.
    can_focus = True
    BINDINGS = [
        Binding("up", "body_up", show=False),
        Binding("down", "body_down", show=False),
        Binding("pageup", "body_page_up", show=False),
        Binding("pagedown", "body_page_down", show=False),
        Binding("home", "body_home", show=False),
        Binding("end", "body_end", show=False),
    ]

    def _scroll_body(self, action: str) -> None:
        for widget in self.query(VerticalScroll):
            if widget.max_scroll_y > 0:
                getattr(widget, action)(animate=False)
                break

    def action_body_up(self) -> None:
        self._scroll_body("scroll_up")

    def action_body_down(self) -> None:
        self._scroll_body("scroll_down")

    def action_body_page_up(self) -> None:
        self._scroll_body("scroll_page_up")

    def action_body_page_down(self) -> None:
        self._scroll_body("scroll_page_down")

    def action_body_home(self) -> None:
        self._scroll_body("scroll_home")

    def action_body_end(self) -> None:
        self._scroll_body("scroll_end")

    #: Digit key that selects this panel.
    KEY: str = ""
    #: Human-readable title shown in the tab bar.
    TITLE: str = ""
    #: Seconds between samples.
    INTERVAL: float = 1.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._worker: Worker | None = None
        self._active = False
        self._error: str | None = None
        self._elapsed = 0.0
        self.settings = Settings()

    # -- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield from self.body()
        yield Static("", id="status", classes="statusline")

    @property
    def status_text(self) -> str:
        """The status strip as plain text, used by the tests."""
        try:
            return str(self.query_one("#status", Static).content)
        except Exception:
            return ""

    def body(self) -> Iterable[Widget]:
        """Widgets that make up this panel. Override in subclasses."""
        return ()

    # -- lifecycle --------------------------------------------------------

    def activate(self) -> None:
        """Begin sampling. Called when the panel becomes visible."""
        if self._active:
            return
        self._active = True
        self.on_activate()
        self._worker = self.run_worker(
            self._poll(), name=f"poll-{self.KEY}", group="sysmon-poll", exclusive=True
        )

    def deactivate(self) -> None:
        """Stop sampling. Called when the panel is hidden."""
        if not self._active:
            return
        self._active = False
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        self.on_deactivate()

    def refresh_now(self) -> None:
        """Re-sample immediately, e.g. from the ``r`` binding."""
        if not self._active:
            return
        if self._worker is not None:
            self._worker.cancel()
        self._worker = self.run_worker(
            self._poll(), name=f"poll-{self.KEY}", group="sysmon-poll", exclusive=True
        )

    async def _poll(self) -> None:
        while self._active:
            started = time.monotonic()
            try:
                data = await asyncio.to_thread(SOURCES.collect, self.fetch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad sample must not kill the panel
                if hasattr(self.app, "history"):
                    self.app.history.failed(self.KEY)
                self._report_error(f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(self.INTERVAL)
                continue

            self._error = None
            try:
                self.apply(data)
                if hasattr(self.app, "history"):
                    self.app.history.record(self.KEY, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._report_error(f"render: {type(exc).__name__}: {exc}")

            self._elapsed = time.monotonic() - started
            self._refresh_status()
            # Keep the cadence honest by subtracting the work already done.
            await asyncio.sleep(max(0.05, self.INTERVAL - self._elapsed))

    # -- overridable hooks -------------------------------------------------

    def fetch(self):
        """Read the subsystem. Blocking; runs on a worker thread."""
        raise NotImplementedError

    def apply(self, data) -> None:
        """Render a snapshot. Runs on the event loop."""
        raise NotImplementedError

    def on_activate(self) -> None: ...

    def on_deactivate(self) -> None: ...

    def on_error(self, message: str) -> None:
        """Called when a sample or a render failed."""

    # -- helpers -----------------------------------------------------------

    def _report_error(self, message: str) -> None:
        self._error = message
        self.on_error(message)
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            status = self.query_one("#status", Static)
        except Exception:  # panel not mounted yet
            return
        status.remove_class("error")
        if self._error:
            status.add_class("error")
            status.update(Text.assemble(("error: ", "bold red"), (self._error, "red")))
        else:
            status.update(
                Text.assemble(
                    ("sample ", "grey42"),
                    (f"{self._elapsed * 1000:.0f}ms", "grey62"),
                    (f"  every {self.INTERVAL:g}s", "grey35"),
                )
            )

    def status_prefix(self) -> str:
        """Extra context shown on the left of the status line."""
        return ""
