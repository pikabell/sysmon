# Bug and UI audit — 2026-10-11

This audit adds 25 regression tests and four UI checks to the existing 47-test suite.

## Corrected behavior

| Area | Failure | Change |
| --- | --- | --- |
| Settings | Huge numeric values could raise an uncaught overflow; booleans were accepted as numbers | Validate numeric types and report configuration errors |
| Config location | Empty or relative `XDG_CONFIG_HOME` could resolve into the working directory | Fall back to `~/.config` |
| Export | An unresolved `~user` path could crash the CLI or dialog | Validate before collection; show an error and allow retry |
| Tables | Updated commands or values could exceed their original column width | Recalculate width when a cell changes |
| History | Empty selection could crash; new metrics did not appear; disappeared devices left stale history | Handle an empty selection, refresh options and append unavailable gaps |
| CPU | Missing or malformed aggregate counters could appear as zero usage or raise an index error | Reject invalid aggregate counters; skip malformed optional cores |
| Network | One malformed interface or socket row could abort valid data | Keep valid rows and report partial interface errors |
| Processes | PID reuse during decoration could combine one process's counters with another's command | Verify start ticks again after decoration and reset rejected baselines |
| Timestamps | Non-finite boot time or uptime could poison rendering and exports | Leave invalid timestamps unavailable |
| Cgroups | Malformed membership could retain an old baseline; a narrower mount could hide ancestor caps | Reset stale baselines and select a containing mount with visible ancestors |
| Cgroup CPU limits | Empty or malformed limits could appear unlimited | Validate quota and period, preserving unknown capacity |
| Hybrid cgroups | A v2 systemd hierarchy could mask legacy CPU/memory controllers | Read the applicable v1 controllers and identify the hybrid hierarchy |
| NVIDIA ABI | Version-dependent process structures could mismatch a ctypes buffer | Use explicit NVML v2 process APIs |
| NVIDIA devices | One inaccessible GPU could hide other devices or change their indices | Skip inaccessible handles and retain driver indices |
| NVIDIA MIG accounting | Separate instance allocations could collapse into a single maximum | Deduplicate overlapping compute/graphics records per instance, then sum instances |

NVML uses the structure defined in NVIDIA's [official header](https://github.com/NVIDIA/go-nvml/blob/main/pkg/nvml/nvml.h).
The choice of explicit v2 process APIs also follows NVIDIA's
[ABI compatibility guidance](https://forums.developer.nvidia.com/t/nvml-12-535-43-02-breaks-backwards-compatibility/254999).

## UI changes

- Added the `sysmon-dark` palette, clearer table headers, subtle alternating rows
  and distinct focus states.
- Added the active panel name to the header and hid unrelated footer actions.
- Placed network traffic beside process rankings in the wide overview.
- Sized the disk device table to its rows, leaving room for filesystem capacity.
- Right-aligned disk values and separated read/write colors.
- Kept settings and export buttons visible while their fields scroll.
- Shortened dialog labels and regenerated previews for every panel and settings.

Fixtures check contextual actions, light-theme layout, disk table sizing and pinned
settings buttons at 50×16. Existing tests cover resizing, navigation, filter typing,
stable overview geometry, inspection and exports.

## Validation scope

The release checks and reproduction commands are in [DEVELOPMENT.md](DEVELOPMENT.md)
and [RELEASE.md](../RELEASE.md). Fixture tests cover MIG accounting and hybrid
cgroups; they do not establish behavior on real MIG, AMD or Intel hardware.
Live NVIDIA checks use four Quadro RTX 5000 devices. Permission-restricted counters
and hidden cgroup ancestors remain unavailable by design.
