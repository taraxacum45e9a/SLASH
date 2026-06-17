import os
from pathlib import Path
from typing import Dict


def _environment_with_udev_ld_preload() -> Dict[str, str]:
    """
    Create a dictionary of environment variables (based on the current one),
    that works around a weird issue when running Vivado in a container.

    Details:
    https://adaptivesupport.amd.com/s/question/0D54U00005Sgst2SAB/failed-batch-mode-execution-in-linux-docker-running-under-windows-host?language=en_US
    https://community.flexera.com/t5/InstallAnywhere-Forum/Issues-when-running-Xilinx-tools-or-Other-vendor-tools-in-docker/m-p/245820#M10647
    """
    possible_paths = [
        Path("/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/lib64/libudev.so.1"),
    ]
    existing_paths = [str(path) for path in possible_paths if path.is_file()]
    env = dict(os.environ)
    if len(existing_paths) > 0:
        env["LD_PRELOAD"] = ":".join(existing_paths)
    return env
