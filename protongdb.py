#!/usr/bin/env python3
# Debug a Steam Play/Proton game by launching it UNDER GDB from the start.
#
# This avoids the race of "launch, find pid, attach later".
# It launches wine under gdb and stops at exec before the Windows program runs.
#
# Depends on ProtonTricks (GPLv3), so this script is GPLv3 as well.

import argparse
import logging
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

from protontricks import *

logger = logging.getLogger("protongdb")


def enable_logging(info=False):
    level = logging.INFO if info else logging.WARNING
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(name)s (%(levelname)s): %(message)s",
    )


def normalize_path(path):
    if not path:
        return ""
    return path.replace("\\", "/")


def get_launch_executable(appid, appinfo):
    app_infos = []
    for app in appinfo:
        if app["appinfo"]["appid"] == appid:
            launch_infos = app["appinfo"]["config"]["launch"]
            for launch_info in launch_infos.values():
                if (
                    "config" not in launch_info
                    or "oslist" not in launch_info["config"]
                    or "windows" in launch_info["config"]["oslist"]
                ):
                    beta_key = None
                    if "config" in launch_info:
                        beta_key = launch_info["config"].get("betakey")
                    arguments = launch_info.get("arguments")
                    arguments = arguments.split() if arguments else []
                    app_infos.append(
                        (
                            launch_info.get("description"),
                            normalize_path(launch_info.get("workingdir")),
                            normalize_path(launch_info["executable"]),
                            beta_key,
                            arguments,
                        )
                    )
    return app_infos


def prepend_args(x, y, delim):
    return (y + delim + x) if y else x


def append_args(x, y, delim):
    return (x + delim + y) if y else x


def list_to_space_str(lst):
    return " ".join(lst)


def shell_join(args):
    return " ".join(shlex.quote(str(x)) for x in args)


def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default


def verify_tools():
    required = ["gdb"]
    missing = []
    for tool in required:
        if subprocess.call(
            ["sh", "-c", f"command -v {shlex.quote(tool)} >/dev/null 2>&1"]
        ) != 0:
            missing.append(tool)
    return missing


def download_winereload():
    url = (
        "https://gist.githubusercontent.com/rbernon/"
        "cdbdc1b0e892f91e7449fcf3dda80bb7/raw/"
        "d8cf549bf751d99ed0fe515e36f99ff5c01b7287/WineReload.py"
    )
    dest = "/tmp/winereload.py"
    urllib.request.urlretrieve(url, dest)
    return dest


def write_gdb_script(path: Path, extra_breaks=None, auto_continue=False):
    if extra_breaks is None:
        extra_breaks = []

    cmds = [
        "set confirm off",
        "set pagination off",
        "set print thread-events off",
        "set breakpoint pending on",
        "set debuginfod enabled off",
        "set detach-on-fork off",
        "set follow-fork-mode child",
        "set follow-exec-mode new",
        "catch exec",
        "catch fork",
        "catch vfork",
        "catch syscall execve",
        "handle SIGUSR1 noprint nostop pass",
        "handle SIGSYS noprint nostop pass",
        "handle SIGPIPE noprint nostop pass",
        "handle SIG32 noprint nostop pass",
        "handle SIG33 noprint nostop pass",
        "catch signal SIGSEGV",
        "catch signal SIGABRT",
        "catch throw",
    ]

    if os.path.exists("/tmp/winereload.py"):
        cmds.append("source /tmp/winereload.py")

    # Pending breakpoints: if symbols appear later, gdb will bind them.
    cmds.extend([
        "break main",
        "break WinMain",
        "break SDL_main",
        "break abort",
        "break exit",
    ])

    cmds.extend(extra_breaks)

    cmds.append('echo Launched under GDB. Target is stopped under debugger control.\\n')
    cmds.append('echo Use "run" to start, then "step"/"next"/"continue".\\n')
    cmds.append('echo On crash, run "wine-reload" and then "thread apply all bt full".\\n')

    if auto_continue:
        cmds.append("run")

    with open(path, "w") as f:
        for c in cmds:
            f.write(c + "\n")


def main(args=None):
    parser = argparse.ArgumentParser(
        description="Launch a Steam Play/Proton game under GDB from the start.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--auto-run", action="store_true", help="automatically issue 'run' inside gdb")
    parser.add_argument("--breakpoint", "-b", action="append", default=[], help="extra breakpoint to set in gdb")
    parser.add_argument("--force-windowed", action="store_true", default=True, help="add common windowed launch args")
    parser.add_argument("appid", type=int, nargs="?", default=None)
    parser.add_argument("app_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(args)

    enable_logging(args.verbose)

    os.environ["DEBUGINFOD_URLS"] = ""

    if not args.appid:
        parser.print_help()
        return 1

    missing = verify_tools()
    if missing:
        logger.error("Missing required tools: %s", ", ".join(missing))
        return 1

    steam_path, steam_root = find_steam_path()
    steam_lib_paths = get_steam_lib_paths(steam_path)
    if not steam_lib_paths:
        logger.error("Could not find Steam lib paths.")
        return 1

    steam_apps = get_steam_apps(steam_root, steam_path, steam_lib_paths)
    if not steam_apps:
        logger.error("Could not find Steam apps.")
        return 1

    game_app = None
    for steam_app in steam_apps:
        if steam_app.appid == args.appid:
            game_app = steam_app
            break

    if not game_app:
        logger.error("Cannot find game with appid: %s", args.appid)
        return 1

    if not game_app.prefix_path:
        logger.error("Cannot find prefix for appid: %s", args.appid)
        return 1

    proton_app = find_proton_app(steam_path, steam_apps, args.appid)
    if not proton_app:
        logger.error("Cannot find a Proton app for appid: %s", args.appid)
        return 1

    appinfo_path = steam_path / "appcache" / "appinfo.vdf"
    appinfo = get_appinfo_sections(appinfo_path)
    if not appinfo:
        logger.error("Cannot find appinfo at %s", appinfo_path)
        return 1

    app_infos = get_launch_executable(args.appid, appinfo)
    if not app_infos:
        logger.error("Cannot find launch executable from %s", appinfo_path)
        return 1

    # Always auto-select config 0
    _, working_dir_rel, launch_executable_rel, beta_key, app_config_args = app_infos[0]

    try:
        download_winereload()
    except Exception as e:
        logger.warning("Failed to download WineReload.py: %s", e)

    gdb_script_path = Path("/tmp/.protongdb_gdbinit")
    write_gdb_script(gdb_script_path, extra_breaks=args.breakpoint, auto_continue=args.auto_run)

    executable_path = game_app.install_path / launch_executable_rel
    working_dir = game_app.install_path / working_dir_rel if working_dir_rel else game_app.install_path

    forced_windowed_args = []
    if args.force_windowed:
        forced_windowed_args = [
            "-windowed",
            "-noborder",
        ]

    app_args = app_config_args + forced_windowed_args + args.app_args

    print(f"Proton: {proton_app.name} ({proton_app.appid})")
    print(f"App: {game_app.name} ({game_app.appid})")
    print(f"Using install dir: {game_app.install_path}")
    print(f"Using Proton prefix: {game_app.prefix_path}")
    print("--------------------------------------------")
    print(f"Using working dir: {working_dir}")
    print(f"Using launch executable: {executable_path}")
    print(f"Using arguments: {list_to_space_str(app_args)}")
    print("--------------------------------------------")
    print("This version launches the game under GDB from the beginning.")
    print("That means startup code is under debugger control from the start.")
    print("At the GDB prompt, use:")
    print("  run")
    print("then step/next/continue as needed.")
    print()

    env_vars = dict(os.environ)
    env_vars["DEBUGINFOD_URLS"] = ""
    env_vars["PATH"] = append_args(f"{proton_app.install_path}/files/bin", env_vars.get("PATH"), ":")
    env_vars.setdefault("WINEDEBUG", "-all")
    env_vars["WINEDLLPATH"] = prepend_args(
        f"{proton_app.install_path}/files/lib64/wine:{proton_app.install_path}/files/lib/wine",
        env_vars.get("WINEDLLPATH"),
        ":",
    )
    env_vars["LD_LIBRARY_PATH"] = append_args(
        f"{proton_app.install_path}/files/lib64/:{proton_app.install_path}/files/lib/:{game_app.install_path}",
        env_vars.get("LD_LIBRARY_PATH"),
        ":",
    )
    env_vars.setdefault("WINEPREFIX", str(game_app.prefix_path))
    env_vars.setdefault("WINEESYNC", "1")
    env_vars.setdefault("WINEFSYNC", "1")
    env_vars.setdefault("SteamGameId", str(args.appid))
    env_vars.setdefault("SteamAppId", str(args.appid))
    env_vars["WINEDLLOVERRIDES"] = append_args(
        "steam.exe=b;dotnetfx35.exe=b;dxvk_config=n;d3d11=n;d3d10=n;d3d10core=n;d3d10_1=n;d3d9=n;dxgi=n",
        env_vars.get("WINEDLLOVERRIDES"),
        ";",
    )
    env_vars.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(steam_path))
    env_vars.setdefault("WINE_LARGE_ADDRESS_AWARE", "1")
    env_vars["GST_PLUGIN_SYSTEM_PATH_1_0"] = prepend_args(
        f"{proton_app.install_path}/files/lib64/gstreamer-1.0:{proton_app.install_path}/files/lib/gstreamer-1.0",
        env_vars.get("GST_PLUGIN_SYSTEM_PATH_1_0"),
        ":",
    )
    env_vars.setdefault("WINE_GST_REGISTRY_DIR", f"{game_app.prefix_path}/gstreamer-1.0/")

    # We debug wine itself from the start.
    wine_binary = f"{proton_app.install_path}/files/bin/wine"
    target_args = ["steam.exe", str(executable_path)] + app_args

    gdb_cmd = [
        "gdb",
        "-q",
        "-iex", "set debuginfod enabled off",
        "-x", str(gdb_script_path),
        "--args",
        wine_binary,
        *target_args,
    ]

    print("Launching GDB with command:")
    print(" ", shell_join(gdb_cmd))
    print()
    print("Inside GDB:")
    print("  run")
    print("to start the game under debugger control.")
    print()

    rc = subprocess.call(
        gdb_cmd,
        cwd=str(working_dir),
        env=env_vars,
    )

    try:
        gdb_script_path.unlink(missing_ok=True)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
