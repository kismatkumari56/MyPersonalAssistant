"""Pytest configuration and environment initialization."""

import os
import sys

# Ensure Tcl/Tk paths on Windows use forward slashes so Tcl can locate init.tcl across repeated test executions.
if sys.platform == "win32":
    tcl_dir = os.path.join(sys.prefix, "tcl", "tcl8.6").replace("\\", "/")
    tk_dir = os.path.join(sys.prefix, "tcl", "tk8.6").replace("\\", "/")
    if os.path.isdir(tcl_dir):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ["TK_LIBRARY"] = tk_dir

