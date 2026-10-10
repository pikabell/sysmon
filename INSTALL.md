# Installation

Linux, Python 3.10+, readable `/proc`, and a UTF-8 terminal are required.
An 80×24 terminal is recommended. SSH works with a TTY allocated. No root privileges,
compiler or GPU hardware is required for the base application.

## Source checkout

```sh
git clone https://github.com/pikabell/sysmon.git
cd sysmon
uv sync
uv run sysmon
```

To install the public source directly: `uv tool install git+https://github.com/pikabell/sysmon.git`.

To install on your PATH in an isolated environment:

```sh
uv tool install .
sysmon --check
sysmon
```

Or use pip in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/sysmon --check
.venv/bin/sysmon
```

If your Python lacks venv/pip, install those through the distribution's package
manager or use uv with a managed Python. Avoid modifying system Python with sudo pip.

## Local wheel

```sh
python3 -m venv ~/.local/share/sysmon-venv
~/.local/share/sysmon-venv/bin/pip install ./sysmon-0.1.0-py3-none-any.whl
~/.local/share/sysmon-venv/bin/sysmon --check
~/.local/share/sysmon-venv/bin/sysmon
```

The wheel includes the terminal stylesheet. Launching from the source directory
is unnecessary. No package-registry release has been made by this project.

## Optional socket accelerator

The base reader uses procfs. For faster socket enumeration:

```sh
uv tool install '.[fast-sockets]'
```

Or `.venv/bin/pip install '.[fast-sockets]'` in an existing environment.
This adds psutil. Where a compatible wheel is unavailable, this optional extra may
need a C compiler/Python headers. The base package remains usable without it.

## Optional GPUs

NVIDIA: the driver must expose NVML (`libnvidia-ml.so.1`). CUDA development tools
are unnecessary. Native process queries provide VRAM ownership; unsupported queries
try `nvidia-smi pmon` with a timeout and reduced cadence. Failed process reads do
not prevent device telemetry.

AMD: AMDGPU can expose busy/VRAM and hwmon counters under
`/sys/class/drm/card*/device`. No AMD user-space utility is required.

Intel/other DRM: identity and readable sensors are shown. Unsupported utilisation
and VRAM stay unknown. GPU process accounting via sysfs is not implemented.
GPU support depends on hardware/driver interfaces and is optional for the app.

## Troubleshooting

Run `sysmon --check` first: exit 0 means required counters are readable; exit 2
means a required capability is missing. Optional GPUs are not required to pass.

| Symptom | Check |
| --- | --- |
| CPU/memory counters unavailable | Procfs mounted/readable at `/proc` |
| Missing processes/socket owners | Permissions, hidepid, namespaces |
| GPU `n/a`, `-` or `?` | That metric may be unsupported; it means unknown |
| No GPU telemetry | Driver/readable sysfs or NVIDIA NVML |
| CPU/RAM larger than container quota | Gauges use procfs totals, not cgroup quotas |
| Need container-sized percentages | Panel 7 shows cgroup usage against effective visible limits |
| Content below the screen | Up/Down, PgUp/PgDn, Home/End or mouse wheel |
| Columns off screen | Shift+Left/Right or horizontal scrollbar |
| Editing numeric process filter | Press `/`; digits/arrows edit until Enter/Esc |
| Broken glyphs | UTF-8 locale and a Unicode-capable terminal font |

After source edits, update a tool installation with `uv tool install --force .`.
Uninstall it with `uv tool uninstall sysmon`. Virtual environments are independent
of the system Python.

## Configuration and reports

Press `,` to edit and save refresh cadence, startup panel, theme, hidden interfaces,
history length and warning thresholds. Defaults load without creating files.
Settings live in `$XDG_CONFIG_HOME/sysmon/config.json` (normally
`~/.config/sysmon/config.json`). `--config PATH` selects another location.

```sh
sysmon --panel 7 --refresh 0.5
sysmon --export ./report.json
sysmon --export ./report-with-commands.json --include-commands
```

Existing report files are not overwritten. Reports use owner-only permissions and
contain visible host/process/network metadata. Commands are omitted by default;
review any report before sharing. Neither reports nor settings are uploaded.

All features run without sudo. Detailed reads for other users' processes can be
denied. PSI requires kernel support. Cgroup views cover the monitor's visible
hierarchy, including visible ancestor constraints; hidden ancestors cannot be read.
Disk I/O reads procfs without opening devices. Remote/unsupported filesystem
capacity queries are skipped to avoid stalled mounts hanging the collector.
