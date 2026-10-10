# Follow-up work

The requested release feature set is implemented: disk activity/capacity/inodes,
PSI, cgroup-aware accounting, process inspection, expanded history, metric help,
saved configuration, explicit private JSON exports, GPU job start times and uptime.
Monitoring never changes process state or system resource controls. Settings and
reports are local writes initiated by the user.

Future work beyond this release:

- Hardware checks on AMD/Intel GPUs and big-endian systems.
- Driver-specific thermal limits and supported Intel utilisation.
- Additional filesystems and bounded remote capacity queries.
- Optional persistent history or time-based graph aggregation.
- Cross-container views with explicit permission handling.

Native NVML usually provides process VRAM, not SM/memory activity; unsupported
readings stay unknown. Sysfs GPU process accounting is not available. Hidden
cgroup ancestors, restricted procfs and permissions limit visibility.

[RELEASE.md](RELEASE.md) records validation and publication scope.
