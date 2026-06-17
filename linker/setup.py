import argparse
import importlib.resources as resources
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from slashkit.core.command_config import CommandConfiguration
from slashkit.emit.hw import _environment_with_udev_ld_preload


logger = logging.getLogger(__name__)


AVED_DESIGN_NAME = "amd_v80_gen5x8_25.1"


# Host toolchain flags injected by dpkg-buildpackage (e.g. -mno-omit-leaf-frame-pointer,
# -fcf-protection, -fstack-clash-protection) are not understood by the arm-xilinx-eabi
# cross-compiler used for the AVED AMC firmware. Strip them before shelling out.
_CROSS_BUILD_ENV_BLOCKLIST = (
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "FFLAGS",
    "FCFLAGS",
    "OBJCFLAGS",
    "OBJCXXFLAGS",
    "GCJFLAGS",
    "ASFLAGS",
)


def _clean_cross_build_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _CROSS_BUILD_ENV_BLOCKLIST}
    return {k: v for k, v in env.items() if not k.startswith("DEB_")}


def _copy_checked(src: Path, dest: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Expected file not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _copy_files(src_files: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for src in src_files:
        dst = destination / src.name
        # Allow install_dir to match the staging directory without failing on no-op copies.
        if dst.exists():
            try:
                if src.samefile(dst):
                    logger.info(
                        "Skipping copy because source and destination are the same file: %s", src)
                    continue
            except FileNotFoundError:
                pass
        shutil.copy2(src, dst)


def _copy_tree(src_dir: Path, destination: Path) -> None:
    target_dir = destination / src_dir.name
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, target_dir, dirs_exist_ok=True)


def _ensure_boot_device_pcie_in_bif(bif_path: Path) -> None:
    if not bif_path.exists():
        raise FileNotFoundError(f"Expected BIF file not found: {bif_path}")

    lines = bif_path.read_text().splitlines()
    if any(line.strip() == "boot_device { pcie }" for line in lines):
        return

    # Find id=0x2
    pattern = re.compile(r"^(\s*)id\s*=\s*0x2\s*$")
    for idx, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            lines.insert(idx + 1, f"{match.group(1)}boot_device {{ pcie }}")
            bif_path.write_text("\n".join(lines) + "\n")
            return

    raise ValueError(f"Could not find 'id = 0x2' in BIF file: {bif_path}")


def _generate_top_wrapper_pdi_with_bootgen(impl_dir: Path) -> Path:
    bif_path = impl_dir / "top_wrapper.bif"
    output_pdi = impl_dir / "top_wrapper.pdi"

    _ensure_boot_device_pcie_in_bif(bif_path)
    logger.info("Running bootgen in %s to generate %s",
                impl_dir, output_pdi.name)
    subprocess.run(
        [
            "bootgen",
            "-arch",
            "versal",
            "-image",
            bif_path.name,
            "-w",
            "-o",
            output_pdi.name,
        ],
        cwd=str(impl_dir),
        check=True,
    )

    if not output_pdi.exists():
        raise FileNotFoundError(
            f"Expected bootgen output not found: {output_pdi}")
    return output_pdi


def generate_base_pdi_with_aved(config: CommandConfiguration) -> Path:
    aved_dir = config.build_dir / "AVED"

    aved_hw_dir = aved_dir / "hw" / AVED_DESIGN_NAME
    aved_build_dir = aved_hw_dir / "build"
    aved_fpt_dir = aved_hw_dir / "fpt"
    aved_fw_profile_dir = aved_dir / "fw" / "AMC" / \
        "src" / "profiles" / "v80"

    logger.info("Starting AVED base build for %s", config.project_name)
    aved_build_dir.mkdir(parents=True, exist_ok=True)

    static_impl_dir = config.build_dir / "slash.runs" / "impl_1"
    regenerated_top_wrapper_pdi = _generate_top_wrapper_pdi_with_bootgen(
        static_impl_dir)
    _copy_checked(regenerated_top_wrapper_pdi,
                  aved_build_dir / "top_wrapper.pdi")

    files_to_copy = [("build_all.sh", aved_hw_dir), ("profile_hal.h", aved_fw_profile_dir),
                     ("pdi_combine.bif", aved_fpt_dir), (f"{AVED_DESIGN_NAME}.xsa", aved_build_dir)]

    for (file_name, target_dir) in files_to_copy:
        with resources.path("slashkit.resources.aved", file_name) as in_path:
            _copy_checked(in_path, target_dir / file_name)

    logger.info("Running AVED build script in %s", aved_hw_dir)
    subprocess.run(
        ["bash", "build_all.sh"],
        cwd=str(aved_hw_dir),
        env=_clean_cross_build_env(),
        check=True,
    )

    aved_pdi = aved_hw_dir / f"{AVED_DESIGN_NAME}.pdi"
    if not aved_pdi.exists():
        raise FileNotFoundError(f"Expected AVED output not found: {aved_pdi}")
    logger.info("AVED fallback complete. Generated %s", aved_pdi)
    return aved_pdi


def create_build_project(
    config: CommandConfiguration,
    action: Optional[str] = None
) -> None:
    log_path = config.build_dir / "vivado.log"

    with resources.path("slashkit.resources.base.scripts", "create_project.tcl") as tcl_path:
        if not tcl_path.exists():
            raise FileNotFoundError(
                f"create_project.tcl not found: {tcl_path}")
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
            config.project_name,
        ]

        if config.ip_repository.exists():
            cmd.append(config.ip_repository)

        if action:
            cmd.append(action)

        cmd.append(str(config.n_jobs))

        subprocess.run(cmd, cwd=str(config.build_dir), check=True,
                       env=_environment_with_udev_ld_preload())


def install_static_shell(config: InstallerConfiguration) -> None:
    static_shell_dir = config.out_dir / "static_shell"
    static_shell_dir.mkdir(parents=True, exist_ok=True)

    # Cloning the AVED repository into the build directory
    # We're doing this early so that errors are caught *before* the 10-hour Vivado run!
    subprocess.run([
        "git", "clone",
        "--recurse-submodules",
        "-b", config.aved_ref,
        config.aved_repo,
        config.build_dir / "AVED"
    ], check=True)

    create_build_project(config)

    impl_dir = config.build_dir / "slash.runs" / "impl_1"
    dcp_sources = (
        impl_dir / "top_wrapper_routed_bb.dcp",
        impl_dir / "static_shell_slash.dcp",
        impl_dir / "static_shell_service_layer.dcp",
    )
    for src in dcp_sources:
        if not src.exists():
            raise FileNotFoundError(
                f"Expected install artifact not found: {src}")
    _copy_files(list(dcp_sources), static_shell_dir)

    src_dirs = config.build_dir / "slash.srcs" / "sources_1" / "bd"
    for src_dir in (src_dirs / "slash_base", src_dirs / "service_layer"):
        if not src_dir.is_dir():
            raise FileNotFoundError(
                f"Expected install BD directory not found: {src_dir}")
        _copy_tree(src_dir, static_shell_dir)

    aved_pdi_path = generate_base_pdi_with_aved(config)
    if not aved_pdi_path.exists():
        raise FileNotFoundError(
            f"Expected AVED PDI not found in results/base: {aved_pdi_path}")
    _copy_files([aved_pdi_path], static_shell_dir)

    def add_init_files(path: Path):
        (path / "__init__.py").touch()
        for sub_path in path.iterdir():
            if not sub_path.is_dir():
                continue
            add_init_files(sub_path)
    add_init_files(static_shell_dir)


####################################################################


INSTALL_HELP_EPILOG = f"""
Purpose:
  The 'install' subcommand builds the static shell required for
  hardware builds. This is a one-time setup operation that creates base images
  used by the 'link' subcommand when targeting hardware (-p hw).

When to Use:
  - During initial installation and/or packaging of slashkit
  - When the static shell definition needs to be regenerated

  Most users will NOT need to run this command regularly. It is only required
  during linker installation/setup.

What It Does:
  1. Builds the static shell base images from the resource directory
  2. Generates necessary Vivado synthesis artifacts
  3. Creates reusable partial designs for hardware linking

  WARNING: This operation involves full Vivado synthesis and implementation,
  which takes significant time (multiple hours depending on the system).

Build Artifacts:
  The build directory (--build-dir) will contain Vivado projects, checkpoints,
  and logs. This directory can be removed after successful installation.

Example:
  {sys.argv[0]} install --build-dir ./install.prj --jobs 16 --out-dir linker/slashkit/resources
"""


class InstallerConfiguration(CommandConfiguration):
    @classmethod
    def populate_argument_parser(cls, ap: argparse.ArgumentParser):
        super().populate_argument_parser(ap)
        ap.description = "Build and install base images for hardware builds."
        ap.epilog = INSTALL_HELP_EPILOG
        ap.add_argument("--build-dir", required=False, type=Path, default=Path(
            "./install.prj"), help="The build directory for the installer. Default: ./install_prj")
        ap.add_argument("--aved-repo", required=False, type=str, default="https://github.com/Xilinx/AVED.git",
                        help="The AVED git repository to check out. Default: https://github.com/Xilinx/AVED.git")
        ap.add_argument("--aved-ref", required=False, type=str, default="amd_v80_gen5x8_25.1_xbtest_20251113",
                        help="The AVED git ref to check out. Default: amd_v80_gen5x8_25.1_xbtest_20251113")
        ap.add_argument("--out-dir", required=True, type=Path,
                        help="The resource directory to install the artifacts to. "
                        + "If you have checked out the SLASH repository, this would be linker/slashkit/resources")

    def __init__(self, args: argparse.Namespace):
        super().__init__(args)

        self._build_dir: Path = args.build_dir.expanduser().resolve()
        if self._build_dir.is_dir():
            shutil.rmtree(self._build_dir)
        self._build_dir.mkdir(parents=True)

        self._aved_repo: str = args.aved_repo
        self._aved_ref: str = args.aved_ref

        self._out_dir: Path = args.out_dir.expanduser().resolve()
        if not self._out_dir.is_dir():
            raise FileNotFoundError(self._out_dir)

    @property
    def project_name(self) -> str:
        return "slash_install"

    @property
    def build_dir(self) -> Path:
        return self._build_dir

    @property
    def aved_repo(self) -> str:
        return self._aved_repo

    @property
    def aved_ref(self) -> str:
        return self._aved_ref

    @property
    def out_dir(self) -> Path:
        return self._out_dir
