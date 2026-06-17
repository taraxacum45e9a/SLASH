# ##################################################################################################
#  The MIT License (MIT)
#  Copyright (c) 2025-2026 Advanced Micro Devices, Inc. All rights reserved.
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy of this software
#  and associated documentation files (the "Software"), to deal in the Software without restriction,
#  including without limitation the rights to use, copy, modify, merge, publish, distribute,
#  sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all copies or
#  substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# ##################################################################################################
from __future__ import annotations

from enum import Enum
import logging
import shutil
import subprocess
import importlib.resources as resources
from contextlib import ExitStack

from slashkit.emit.hw import _environment_with_udev_ld_preload
from slashkit.emit.metadata.report_util import convert_report_utilization_to_xml
from slashkit.emit.render import export_package
from slashkit.core.command_config import LinkerConfiguration, CommandConfiguration

logger = logging.getLogger(__name__)


class RM_KIND(Enum):
    SLASH_PROJECT = "slash"
    SERVICE_LAYER = "service_layer"


def _run_rm_build(config: LinkerConfiguration, rm_kind: RM_KIND) -> None:
    if rm_kind == RM_KIND.SLASH_PROJECT:
        # Copy all base IP cores into the ip repository
        config.ip_repository.mkdir(parents=True)
        export_package("slashkit.resources.base.iprepo",
                       config.ip_repository / "slash_base")

        # Copy all user kernels into the ip repository
        for kernel in config.kernels:
            shutil.copytree(kernel.component_xml_path.parent,
                            config.ip_repository / kernel.name)
    elif rm_kind == RM_KIND.SERVICE_LAYER and not config.ip_repository.is_dir():
        raise RuntimeError("The IP repository is missing, the user region has to be built before the service layer.\n"
                           "This is a bug, please report it at https://github.com/Xilinx/SLASH")

    logs_dir = config.build_dir / "logs"
    image_out_dir = config.build_dir / "images"
    rm_work_dir = config.build_dir / f"{rm_kind.value}_rm"

    logs_dir.mkdir(parents=True, exist_ok=True)
    image_out_dir.mkdir(parents=True, exist_ok=True)
    rm_work_dir.mkdir(parents=True, exist_ok=True)

    if rm_kind == RM_KIND.SERVICE_LAYER:
        tcl_name = "service_layer_build.tcl"
        static_shell_dcp_name = "static_shell_service_layer.dcp"
        base_bd_package = "slashkit.resources.static_shell.service_layer"
        base_bd_name = "service_layer.bd"
        log_path = logs_dir / "service_layer_build.log"
    else:
        tcl_name = "slash_project_build.tcl"
        static_shell_dcp_name = "static_shell_slash.dcp"
        base_bd_package = "slashkit.resources.static_shell.slash_base"
        base_bd_name = "slash_base.bd"
        log_path = logs_dir / "slash_project_build.log"

    with ExitStack() as stack:
        tcl_path = stack.enter_context(
            resources.path("slashkit.resources.base.scripts", tcl_name)
        )
        static_shell_dcp_path = stack.enter_context(
            resources.path("slashkit.resources.static_shell",
                           static_shell_dcp_name)
        )
        base_bd_path = stack.enter_context(
            resources.path(base_bd_package, base_bd_name)
        )

        cmd = [
            config.vivado_bin,
            "-mode",
            "batch",
            "-nojournal",
            "-log",
            str(log_path),
            "-source",
            str(tcl_path),
            "-tclargs",
            "--project-name",
            config.project_name,
            "--ip-repo",
            str(config.ip_repository),
            "--static-shell-dcp",
            str(static_shell_dcp_path),
            "--base-bd",
            str(base_bd_path),
            "--linker-results-dir",
            str(config.build_dir),
            "--rm-work-dir",
            str(rm_work_dir),
            "--artifact-out-dir",
            str(image_out_dir),
            "--jobs",
            str(config.n_jobs),
        ]
        if rm_kind == RM_KIND.SLASH_PROJECT:
            util_report_path = config.build_dir / \
                f"report_utilization_{config.project_name}.txt"
            util_report_path.parent.mkdir(parents=True, exist_ok=True)
            cmd.extend(["--util-report-file", str(util_report_path)])

            for path in config.pre_synth_tcls:
                cmd.extend(["--pre-synth-tcl", str(path)])

        if rm_kind == RM_KIND.SERVICE_LAYER:
            opt_post_tcl = stack.enter_context(
                resources.path(
                    "slashkit.resources.base.constraints.service_layer.eth", "service_layer_eth.opt.post.tcl")
            )
            cmd.extend(["--opt-post-tcl", str(opt_post_tcl)])

        subprocess.run(cmd, cwd=str(config.build_dir), check=True,
                       env=_environment_with_udev_ld_preload())

    if rm_kind == RM_KIND.SLASH_PROJECT:
        pdi_out_path = image_out_dir / \
            f"top_i_slash_slash_{config.project_name}_inst_0_partial.pdi"
    else:
        pdi_out_path = image_out_dir / \
            f"top_i_service_layer_service_layer_{config.project_name}_inst_0_partial.pdi"

    if not pdi_out_path.is_file():
        raise FileNotFoundError(
            f"{str(pdi_out_path)} is missing! Check {str(log_path)} for errors!")


def build_service_layer_rm(config: LinkerConfiguration) -> None:
    _run_rm_build(config, RM_KIND.SERVICE_LAYER)


def build_slash_rm(config: LinkerConfiguration) -> None:
    _run_rm_build(config, RM_KIND.SLASH_PROJECT)


def generate_util_report(config: CommandConfiguration) -> None:
    report_path = config.build_dir / \
        f"report_utilization_{config.project_name}.txt"
    xml_path = config.build_dir / \
        f"report_utilization_{config.project_name}.xml"
    logger.info("Generating utilization report XML for project %s",
                config.project_name)
    logger.info("Utilization report input: %s", report_path)
    logger.info("Utilization report output: %s", xml_path)
    if not report_path.exists():
        raise FileNotFoundError(report_path)
    convert_report_utilization_to_xml(report_path, xml_path)
    logger.info("Utilization report XML generation complete for %s",
                config.project_name)
