# Optional local outage watcher

This branch includes an optional session-relaunch integration. It is separate
from the core offline Pool messenger. Armed outage detection makes internet
probes; standing mode watches the local Pool book without periodic internet
probes. Configure it only on trusted personal devices.

`outage_watch.py --help` describes the commands. Local configuration lives under
`OUTAGE_HOME`, normally `~/outage` on Linux. `POOL_DIR` selects the local Pool
checkout. Read the code before installing `outage-watch.service`; edit its paths
to your installation. Required executables must already be installed locally.

Relaunch uses fixed argument lists and working directories from local
`relaunch.json` or `resume.json`. The book cannot supply commands or working
directories. The standing trigger is restricted to the operator labels coded
in this version and a specific target device; choose matching local names
deliberately. Repeated entries are tracked by claim stamps.

This integration can start processes with your privileges and pass text to
agents. Review sender labels, configuration permissions, launch commands, and
agent authority before enabling it. A shared Pool key authenticates members
collectively, not the operator label individually. Keep configuration, prompts,
logs, and state out of Git. See the repository [security guide](../SECURITY.md).
