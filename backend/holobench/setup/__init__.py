# SPDX-License-Identifier: GPL-2.0-or-later
"""First-run "build me a board" setup: orchestrates tools/build-me.sh (build the
GPL forked qemu from source + the distributable image) for the web wizard. See
docs/SETUP.md."""
from .manager import (SetupManager, SetupError, required_artifacts,
                      validate_manifest, nxp_manifest, installed_qemu)

__all__ = ["SetupManager", "SetupError", "required_artifacts",
           "validate_manifest", "nxp_manifest", "installed_qemu"]
