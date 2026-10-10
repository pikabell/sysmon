# Release validation

MIT license, copyright 2026 Arjun Badola. Public repository:
https://github.com/pikabell/sysmon. This is a source release; no package-registry
upload is part of this work.

## Feature scope

- Eight panels: overview, CPU, memory, processes, GPU, network, disk, resources.
- Disk I/O/IOPS/busy deltas, filesystem capacity, user-available space and inodes.
- System and cgroup PSI with explicit unsupported/permission states.
- Cgroup v1/v2 CPU and memory gauges using visible ancestor limits and affinity.
- Process inspection: command, parent, status, threads, cumulative I/O, file targets,
  limits, bounded lists and identity checks before/after collection.
- Bounded session history, expanded graphs and contextual metric explanations.
- Saved cadence/default panel/theme/hidden interfaces/thresholds/history length.
- Explicit JSON exports, owner-only files, no overwrites, command redaction by default.
- Full local GPU process start timestamps beside PID; system uptime on every panel.

## Gates

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

Install the wheel into a fresh environment outside the checkout and run the same
regressions against the installed package, without psutil. Confirm packaged styling,
license metadata and all eight panels. `tests/preview.py` creates safe fixture previews.

## Audit evidence (2026-10-10)

Live Ubuntu x86-64 checks run under UID 1000, including CPU/memory/process/network
comparisons with psutil. Disk byte counters matched independent psutil reads on 38
devices. GPU process creation timestamps matched psutil for eight visible jobs on
four NVIDIA Quadro RTX 5000s. Devices/processes can change after these checks.

The audit corrected stale exec command caches, wall-clock-dependent process ages,
network command markup interpretation, history gaps after failed reads, quadratic
GPU process sorting, overlapping header/tab placement, and GPU card width budgets.
PID identity guards, counter resets, CPU guest/iowait accounting, NVML allocation
queries, private report permissions/redaction and non-overwriting writes have tests.

Fixtures cover cgroup v1/v2, inherited fractional CPU/memory limits, cpusets, malformed
limits, namespace boundaries, old/missing kernel fields, AMD/Intel sysfs, unavailable
readings, PID reuse, metric-help/history/details/settings/export interactions,
small-terminal scrolling, and resize/navigation across all panels.

A live Docker container with 0.5 CPUs and 128 MiB memory, running as UID 10001,
reported the exact cgroup limits and nonzero quota-aware CPU usage. No sudo is
required inside the application. Docker access is needed only for reproducing tests.

The expanded suite passed in the following userspaces, without psutil or compilers,
as UID 10001. These share the host Linux kernel; they do not test separate kernels.

| Base image | Python | Base package + regression/UI suite + capability check |
| --- | --- | --- |
| Ubuntu 22.04 | 3.10.12 | Passed |
| Ubuntu 24.04 | 3.12.3 | Passed |
| Debian 12 | 3.11.2 | Passed |
| Fedora 43 | 3.14.7 | Passed |
| Alpine 3.21 (musl) | 3.12.15 | Passed |

The initial release source and installed Python 3.10 base wheel passed **42 tests**, including
all eight panels, dialog workflows, startup settings, unknown job timestamps and
the lifecycle/rendering fixes. Real CLI exports were checked for redaction, private
permissions and overwrite prevention. Static checks and clean archive/license
metadata checks passed. Fixture screenshots were visually reviewed.

### Home refresh audit

Changing active-process, interface and warning counts resized overview cards on
each sample, moving everything below them and clamping the scroll position.
Cards now reserve their height, including missing-counter states, and the notice
area reserves two rows. `n` opens every notice; additional GPUs remain on panel 4.
Rendering uses the actual content width with a reserved scrollbar gutter.
Process sorting/filtering and the GPU-kind toggle now apply to the current sample
immediately, rather than waiting for the next configured refresh.

All **47 tests** passed from source on Python 3.14 and against a fresh Python 3.10
base wheel outside the checkout, without psutil. New tests vary idle/busy samples,
ten warnings, twelve GPUs, missing counters, estimated memory and Unicode labels
across ten widths, and check scroll preservation and immediate table updates.
Five fresh live samples with changing CPU values kept the same layout and scroll
position under UID 1000. Live source comparisons, rendered layouts, eight-panel
navigation, static checks and the lockfile check also passed. Overview previews
were regenerated with safe fixture data and visually reviewed.

## CI

`.github/workflows/test.yml` checks Python 3.10–3.14 on Ubuntu, native Ubuntu ARM64,
five distribution base installs, static correctness and isolated wheel installs.
All **13 jobs** passed in [the public validation run](https://github.com/pikabell/sysmon/actions/runs/38046908825)
for source commit `9c8c764`. Each matrix job ran the final 42-test suite; Python
jobs additionally ran live source, layout and navigation checks. Native ARM64 was
verified as `aarch64`, Python 3.12.15, kernel 6.17.0-1022-azure, independently of
the development host. The workflow now uses current official v7 actions.
Reproduce a distribution gate with:

```sh
docker build --build-arg BASE_IMAGE=alpine:3.21 -f tests/linux.Dockerfile .
```

## Limits

Tests and audit evidence cannot prove absence of every possible bug. Real AMD/Intel
and big-endian hardware remain untested. Restricted procfs, hidden cgroup ancestors,
missing PSI support and vendor interfaces can make readings unavailable. Remote/FUSE
filesystem capacity reads are intentionally skipped. Cgroup capacities are visible
caps, not guaranteed exclusive resources or current memory headroom. Generic GPU
thermal defaults are configurable monitor policy rather than vendor throttling limits.
