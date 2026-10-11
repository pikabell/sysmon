# Development

Run from the repository root on Linux with Python 3.10 or newer:

```sh
uv sync --extra test
uv run --extra test python -m unittest discover -s tests -p 'test_*.py'
uv run --extra test python tests/test_sources.py
uv run --extra test python tests/test_layout.py
uv run --extra test python tests/smoke.py
uvx ruff check --config pyproject.toml src tests
uv lock --check
uv build
```

The unittest suite uses fixtures for hardware, counter failures and UI interaction.
`test_sources.py` compares live readings with psutil. `test_layout.py` checks rendered
widths; `smoke.py` navigates all eight panels using live sources. Live checks depend
on the counters and devices visible to the current user.

## Data flow

`SysmonApp` warms shared sources on a worker thread, then activates the selected
panel. Each panel's polling loop calls `Sources.collect(fetch)` through
`asyncio.to_thread`. The shared lock serializes collection, including a read still
running after its polling worker is cancelled. `apply(snapshot)` runs on the UI
thread, followed by history recording. Hidden panels stop polling.

| Location | Responsibility |
| --- | --- |
| `src/sysmon/sources/` | Read procfs, sysfs and optional vendor APIs; produce snapshots |
| `src/sysmon/core/context.py` | Shared source instances, warm-up and collection lock |
| `src/sysmon/core/panel.py` | Polling, cancellation, errors and status strip |
| `src/sysmon/core/registry.py` | Panel order and navigation keys |
| `src/sysmon/core/table.py` | Incremental rows, selection and column sizing |
| `src/sysmon/panels/` | Snapshot rendering and panel-specific controls |
| `src/sysmon/history.py` | Bounded metric queues and unavailable-sample gaps |
| `src/sysmon/screens.py` | History, inspection, settings and export dialogs |
| `src/sysmon/config.py`, `export.py` | Validated settings and private snapshot files |
| `src/sysmon/sysmon.tcss` | Layout, tables and dialogs; theme colors are registered in `app.py` |

## Collector rules

- Keep telemetry read-only. Never signal processes or write to procfs/sysfs.
- Use monotonic elapsed time for rates. A new or reset counter needs a new baseline.
- Preserve unknown values; an unavailable reading must not become an idle zero.
- Identify processes by PID and kernel start ticks. Recheck identity after reading
  command and ownership data; processes can exit or reuse a PID during collection.
- Use explicit vendor ABI versions matching the ctypes structures. NVIDIA process
  queries use NVML v2; missing support follows the existing pmon fallback.
- Treat kernel names and commands as literal Rich `Text`, rather than markup.
- Keep synchronous reads in `fetch`, and widget changes in `apply`.

## UI changes and previews

The default `sysmon-dark` theme uses muted surfaces, blue selection and cyan accents.
Saved themes still apply. `NO_COLOR` is respected. Wide overview cards retain their
height as values change; narrow terminals scroll. Tables preserve selection during
updates. Footer actions follow the active panel, and settings/export actions sit
outside their scrollable fields.

Generate the documentation previews with synthetic data:

```sh
env -u NO_COLOR uv run python tests/preview.py
```

Review the SVGs in `docs/` at both 150×40 and 80×24. The preview script seeds history
and replaces the settings path with an example. Do not commit live screenshots
containing process commands, users or socket addresses.

When adding a panel, add a source and `Panel` subclass, then register it in `PANELS`.
Update the CLI/config panel range, help text, metric definitions and preview fixtures.
Test navigation, missing data and narrow layouts.

## Installed package check

After building, install the wheel into an environment outside the checkout and run
the fixture suite without psutil:

```sh
uv venv --python 3.10 /tmp/sysmon-wheel-check
uv pip install --python /tmp/sysmon-wheel-check/bin/python dist/sysmon-0.1.0-py3-none-any.whl
cd /tmp
/tmp/sysmon-wheel-check/bin/python -m unittest discover -s /path/to/sysmon/tests -p 'test_*.py'
/tmp/sysmon-wheel-check/bin/sysmon --check
```

This checks the packaged stylesheet, imports, base dependencies and minimum Python
version. CI also runs the suite on Python 3.10–3.14, ARM64 and five Linux userspaces.
