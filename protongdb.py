#!/usr/bin/env python3
import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
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


def list_to_space_str_prefix(lst, prefix):
    return prefix + list_to_space_str(lst) if lst else ""


def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default


def shell_join(args):
    return " ".join(shlex.quote(str(x)) for x in args)


def download_winereload():
    url = (
        "https://gist.githubusercontent.com/rbernon/"
        "cdbdc1b0e892f91e7449fcf3dda80bb7/raw/"
        "d8cf549bf751d99ed0fe515e36f99ff5c01b7287/WineReload.py"
    )
    dest = "/tmp/winereload.py"
    urllib.request.urlretrieve(url, dest)
    return dest


def write_gdb_script(path: Path, auto_continue=False, extra_breaks=None):
    if extra_breaks is None:
        extra_breaks = []

    gdb_commands = [
        "set confirm off",
        "set pagination off",
        "set print thread-events off",
        "set breakpoint pending on",
        "set debuginfod enabled off",
        "set detach-on-fork off",
        "set follow-fork-mode child",
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
        gdb_commands.append("source /tmp/winereload.py")

    gdb_commands.extend([
        "break main",
        "break WinMain",
        "break SDL_main",
        "break abort",
        "break exit",
    ])

    gdb_commands.extend(extra_breaks)

    gdb_commands.append('echo GDB attached. Target should be stopped now. Use "continue", "step", or "next".\\n')
    gdb_commands.append('echo On crash, run "wine-reload" and then "thread apply all bt full".\\n')

    if auto_continue:
        gdb_commands.append("continue")

    with open(path, "w") as f:
        for command in gdb_commands:
            f.write(command + "\n")


def verify_tools():
    required = ["gdb", "pgrep"]
    missing = []
    for tool in required:
        if subprocess.call(
            ["sh", "-c", f"command -v {shlex.quote(tool)} >/dev/null 2>&1"]
        ) != 0:
            missing.append(tool)
    return missing


def collect_candidate_processes(appid, exe_name, install_path, timeout=15.0, verbose=False):
    deadline = time.time() + timeout
    exe_name_l = exe_name.lower()
    install_path_l = str(install_path).lower()
    candidates = {}

    helper_keywords = [
        "wineserver",
        "services.exe",
        "explorer.exe",
        "rpcss.exe",
        "plugplay.exe",
        "steam.exe",
        "conhost.exe",
        "cmd.exe",
    ]

    while time.time() < deadline:
        try:
            output = subprocess.check_output(
                ["pgrep", "-aif", "wine|proton|steam.exe|\\.exe"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            output = ""

        for line in output.splitlines():
            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue
            pid = safe_cast(parts[0], int)
            if pid is None:
                continue
            cmdline = parts[1] if len(parts) > 1 else ""
            cmdline_l = cmdline.lower()

            score = 0
            if exe_name_l in cmdline_l:
                score += 100
            if str(appid) in cmdline_l:
                score += 30
            if install_path_l and install_path_l in cmdline_l:
                score += 50
            if ".exe" in cmdline_l:
                score += 10
            if "wine" in cmdline_l or "proton" in cmdline_l:
                score += 5

            for kw in helper_keywords:
                if kw in cmdline_l:
                    score -= 40

            if score > 0:
                prev = candidates.get(pid)
                if prev is None or score > prev["score"]:
                    candidates[pid] = {"pid": pid, "cmdline": cmdline, "score": score}

        if candidates:
            best = sorted(candidates.values(), key=lambda x: (-x["score"], x["pid"]))
            if verbose:
                logger.info("Current PID candidates:")
                for c in best[:10]:
                    logger.info("  pid=%s score=%s cmd=%s", c["pid"], c["score"], c["cmdline"])
            if best[0]["score"] >= 100:
                return best

        time.sleep(0.2)

    return sorted(candidates.values(), key=lambda x: (-x["score"], x["pid"]))


def main(args=None):
    parser = argparse.ArgumentParser(
        description="Wrapper for debugging Steam Play/Proton games with GDB.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--no-kill", action="store_true", help="do not kill the process after gdb exits")
    parser.add_argument("--attach-timeout", type=float, default=15.0)
    parser.add_argument("--auto-continue", action="store_true", help="automatically continue after attach")
    parser.add_argument("--breakpoint", "-b", action="append", default=[], help="extra breakpoint to set in gdb")
    parser.add_argument("--force-windowed", action="store_true", default=True,
                        help="add common windowed launch args")
    parser.add_argument("appid", type=int, nargs="?", default=None)
    parser.add_argument("app_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(args)

    enable_logging(args.verbose)

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

    # Auto-select launch config 0 if multiple exist
    _, working_dir_rel, launch_executable_rel, beta_key, app_config_args = app_infos[0]

    try:
        download_winereload()
    except Exception as e:
        logger.warning("Failed to download WineReload.py: %s", e)

    gdb_script_path = Path("/tmp/.protongdb_args")
    write_gdb_script(gdb_script_path, auto_continue=args.auto_continue, extra_breaks=args.breakpoint)

    executable_path = game_app.install_path / launch_executable_rel
    working_dir = game_app.install_path / working_dir_rel if working_dir_rel else game_app.install_path

    forced_windowed_args = []
    if args.force_windowed:
        # Common but not universal. Some games ignore some/all of these.
        forced_windowed_args = [
            "-windowed",
            "-noborder",
        ]

    app_args = app_config_args + forced_windowed_args + args.app_args

    print(f"Proton: {proton_app.name} ({proton_app.appid})")
    print(f"App: {game_app.name} ({game_app.appid})")
    print(f"Using working dir: {working_dir}")
    print(f"Using launch executable: {executable_path}")
    print(f"Using arguments: {list_to_space_str(app_args)}")

    env_vars = dict(os.environ)
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

    launch_cmd = [f"{proton_app.install_path}/files/bin/wine", "steam.exe", str(executable_path)] + app_args
    print("Launching:", shell_join(launch_cmd))

    subprocess.Popen(
        launch_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=str(working_dir),
        env=env_vars,
    )

    exe_name = os.path.basename(str(launch_executable_rel))
    candidates = collect_candidate_processes(
        appid=args.appid,
        exe_name=exe_name,
        install_path=game_app.install_path,
        timeout=args.attach_timeout,
        verbose=args.verbose,
    )

    if not candidates:
        logger.error("Couldn't find a suitable PID to attach for %s", exe_name)
        return 1

    chosen = candidates[0]

    print("\nAuto-selected candidate [0]:")
    print(f"pid={chosen['pid']} score={chosen['score']}")
    print(chosen["cmdline"])

    rc = subprocess.call(["gdb", "-x", str(gdb_script_path), "-p", str(chosen["pid"])])

    try:
        gdb_script_path.unlink(missing_ok=True)
    except Exception:
        pass

    if not args.no_kill:
        try:
            os.kill(chosen["pid"], 9)
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
