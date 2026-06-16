# SPDX-License-Identifier: GPL-2.0-or-later
"""v3.0 multi-board topologies (labs): wire boards together over stock QEMU
interfaces (socket/mcast Ethernet now; usbredir later). See docs/TOPOLOGIES.md."""
from .models import Lab, LabError, LabLink, LabNode
from .loader import list_labs, load_lab, load_lab_file
from .coordinator import LabCoordinator, LabState, RunningLab

__all__ = [
    "Lab", "LabError", "LabLink", "LabNode",
    "list_labs", "load_lab", "load_lab_file",
    "LabCoordinator", "LabState", "RunningLab",
]
