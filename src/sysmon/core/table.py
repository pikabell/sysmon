"""Incremental synchronisation of a :class:`DataTable` against fresh rows.

Rebuilding a 650-row table every second is wasteful and causes visible flicker.
:class:`TableSync` instead adds and removes only the rows whose identity changed,
touches only the cells whose rendered text changed, and rebuilds solely when the
sort order itself changes.
"""

from __future__ import annotations

from typing import Hashable, Iterable, Sequence

from textual.widgets import DataTable


class TableSync:
    """Keep a ``DataTable`` aligned with ``{key: cells}``."""

    def __init__(self, table: DataTable, columns: Sequence[tuple[str, str]]) -> None:
        # columns: sequence of (column_key, label)
        self.table = table
        self.columns = tuple(columns)
        self._keys: dict[Hashable, str] = {}
        self._rendered: dict[Hashable, tuple[str, ...]] = {}
        self._order: list[Hashable] = []
        self._force_rebuild = True

    @property
    def order(self) -> list[Hashable]:
        return list(self._order)

    def invalidate(self) -> None:
        """Force a full rebuild on the next sync (e.g. the user changed sort)."""
        self._force_rebuild = True

    def sync(
        self, rows: dict[Hashable, tuple[str, ...]], order: Iterable[Hashable]
    ) -> None:
        order = list(dict.fromkeys(key for key in order if key in rows))
        included = set(order)
        order.extend(key for key in rows if key not in included)
        survivors = [key for key in self._order if key in rows]
        # Appending and removing rows can be done in place; changing their
        # relative position requires a rebuild. Never omit newly sampled rows.
        rebuild = self._force_rebuild or order[: len(survivors)] != survivors

        if rebuild:
            self._rebuild(rows, order)
            return

        cursor_row = self.table.cursor_coordinate.row
        selected_key = (
            self._order[cursor_row] if cursor_row < len(self._order) else None
        )

        # Drop rows whose process has exited.
        for key in [k for k in self._keys if k not in rows]:
            row_key = self._keys.pop(key)
            self._rendered.pop(key, None)
            self.table.remove_row(row_key)

        # Add rows for newly seen processes.
        for key in order:
            if key in self._keys:
                continue
            cells = rows[key]
            self._keys[key] = self.table.add_row(*cells, key=str(key))
            self._rendered[key] = cells

        # Update only cells whose text actually changed.
        for key in order:
            cells = rows[key]
            previous = self._rendered.get(key)
            if previous == cells:
                continue
            row_key = self._keys[key]
            for index, (column, value) in enumerate(
                zip((c for c, _ in self.columns), cells)
            ):
                if previous is None or previous[index] != value:
                    self.table.update_cell(row_key, column, value)
            self._rendered[key] = cells

        self._order = order
        if selected_key in order:
            self.table.move_cursor(row=order.index(selected_key), scroll=False)

    def _rebuild(
        self, rows: dict[Hashable, tuple[str, ...]], order: list[Hashable]
    ) -> None:
        scroll = self.table.scroll_offset
        cursor_row = None
        selected_key = None
        try:
            cursor_row = self.table.cursor_coordinate.row
            if cursor_row < len(self._order):
                selected_key = self._order[cursor_row]
        except Exception:
            pass

        self.table.clear()
        self._keys.clear()
        self._rendered.clear()
        self._order = []

        for key in order:
            cells = rows[key]
            self._keys[key] = self.table.add_row(*cells, key=str(key))
            self._rendered[key] = cells
        self._order = order
        self._force_rebuild = False

        if cursor_row is not None and order:
            try:
                target = (
                    order.index(selected_key)
                    if selected_key in order
                    else min(cursor_row, len(order) - 1)
                )
                self.table.move_cursor(row=target, scroll=False)
            except Exception:
                pass
        self.table.call_after_refresh(
            self.table.scroll_to, x=scroll.x, y=scroll.y, animate=False
        )
