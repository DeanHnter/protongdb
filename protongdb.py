#!/usr/bin/env python3
import argparse
import curses
import datetime
import errno
import fcntl
import logging
import os
import pty
import shlex
import select
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

try:
    from protontricks import (
        find_proton_app,
        find_steam_path,
        get_appinfo_sections,
        get_steam_apps,
        get_steam_lib_paths,
    )
    PROTONTRICKS_IMPORT_ERROR = None
except ImportError as exc:
    PROTONTRICKS_IMPORT_ERROR = exc

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


def set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def sanitize_terminal_text(text):
    cleaned = []
    for ch in text.expandtabs(4):
        if ch == "\n" or ch == "\t" or (" " <= ch <= "~"):
            cleaned.append(ch)
        else:
            cleaned.append("?")
    return "".join(cleaned)


def wrap_lines(lines, width, max_lines):
    if width <= 0 or max_lines <= 0:
        return []

    wrapped = []
    for raw in lines:
        line = sanitize_terminal_text(raw)
        if not line:
            wrapped.append("")
            continue

        while line:
            wrapped.append(line[:width])
            line = line[width:]

    return wrapped[-max_lines:]


def tail_file_lines(path, max_lines=80, block_size=8192):
    if not path.exists():
        return []

    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        remaining = f.tell()
        chunks = []
        newline_count = 0

        while remaining > 0 and newline_count <= max_lines:
            read_size = min(block_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    return data.decode("utf-8", "replace").splitlines()[-max_lines:]


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


def write_gdb_script(path: Path, log_path: Path, extra_breaks=None, auto_run=False):
    if extra_breaks is None:
        extra_breaks = []

    lp = str(log_path).replace("\\", "\\\\")

    cmds = [
        "set confirm off",
        "set pagination off",
        "set print thread-events off",
        "set breakpoint pending on",
        "set debuginfod enabled off",
        "set detach-on-fork off",
        "set follow-fork-mode child",
        "set follow-exec-mode new",
        "set print pretty on",
        "set print object on",
        "set print elements 200",
        "set disassemble-next-line on",
        "set logging file " + lp,
        "set logging overwrite on",
        "set logging redirect off",
        "set logging enabled on",
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

    cmds.extend([
        "break main",
        "break WinMain",
        "break SDL_main",
        "break abort",
        "break exit",
    ])

    cmds.extend(extra_breaks)

    # Helper command: dump current debugger state to log
    cmds.extend([
        "define ilog",
        "  echo \\n========== ILOG STATE DUMP ==========\n",
        "  printf \"PID/TID state dump\\n\"",
        "  info program",
        "  info inferiors",
        "  info threads",
        "  thread",
        "  frame",
        "  where 20",
        "  info args",
        "  info locals",
        "  info registers",
        "  info symbol $pc",
        "  x/20i $pc",
        "  x/32gx $sp",
        "  echo \\n========== END ILOG ==========\n",
        "end",
        "document ilog",
        "Dump current execution state, stack, registers, locals, args, and nearby code.",
        "end",
    ])

    # Step + log
    cmds.extend([
        "define slog",
        "  ilog",
        "  step",
        "end",
        "document slog",
        "Dump state, then step.",
        "end",
    ])

    # Next + log
    cmds.extend([
        "define nlog",
        "  ilog",
        "  next",
        "end",
        "document nlog",
        "Dump state, then next.",
        "end",
    ])

    # Instruction step + log
    cmds.extend([
        "define silog",
        "  ilog",
        "  stepi",
        "end",
        "document silog",
        "Dump state, then step one instruction.",
        "end",
    ])

    # Instruction next + log
    cmds.extend([
        "define nilog",
        "  ilog",
        "  nexti",
        "end",
        "document nilog",
        "Dump state, then next one instruction.",
        "end",
    ])

    # Continue + log
    cmds.extend([
        "define clog",
        "  ilog",
        "  continue",
        "end",
        "document clog",
        "Dump state, then continue.",
        "end",
    ])

    cmds.append('echo Logging enabled. GDB log file: ' + lp + '\\n')
    cmds.append('echo Use "run" to start. Use "ilog", "slog", "nlog", "silog", "nilog", or "clog".\\n')
    cmds.append('echo On crash, run "wine-reload" then "thread apply all bt full".\\n')

    if auto_run:
        cmds.append("run")

    with open(path, "w") as f:
        for c in cmds:
            f.write(c + "\n")


def build_launch_context(args):
    if PROTONTRICKS_IMPORT_ERROR is not None:
        logger.error(
            "Missing required Python dependency: protontricks (%s). Install it with 'pip install --user protontricks'.",
            PROTONTRICKS_IMPORT_ERROR,
        )
        return None

    missing = verify_tools()
    if missing:
        logger.error("Missing required tools: %s", ", ".join(missing))
        return None

    steam_path, steam_root = find_steam_path()
    steam_lib_paths = get_steam_lib_paths(steam_path)
    if not steam_lib_paths:
        logger.error("Could not find Steam lib paths.")
        return None

    steam_apps = get_steam_apps(steam_root, steam_path, steam_lib_paths)
    if not steam_apps:
        logger.error("Could not find Steam apps.")
        return None

    game_app = None
    for steam_app in steam_apps:
        if steam_app.appid == args.appid:
            game_app = steam_app
            break

    if not game_app:
        logger.error("Cannot find game with appid: %s", args.appid)
        return None

    if not game_app.prefix_path:
        logger.error("Cannot find prefix for appid: %s", args.appid)
        return None

    proton_app = find_proton_app(steam_path, steam_apps, args.appid)
    if not proton_app:
        logger.error("Cannot find a Proton app for appid: %s", args.appid)
        return None

    appinfo_path = steam_path / "appcache" / "appinfo.vdf"
    appinfo = get_appinfo_sections(appinfo_path)
    if not appinfo:
        logger.error("Cannot find appinfo at %s", appinfo_path)
        return None

    app_infos = get_launch_executable(args.appid, appinfo)
    if not app_infos:
        logger.error("Cannot find launch executable from %s", appinfo_path)
        return None

    _, working_dir_rel, launch_executable_rel, _, app_config_args = app_infos[0]

    try:
        download_winereload()
    except Exception as e:
        logger.warning("Failed to download WineReload.py: %s", e)

    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"protongdb-{args.appid}-{ts}.log"
    gdb_script_path = Path("/tmp/.protongdb_gdbinit")

    write_gdb_script(
        gdb_script_path,
        log_path=log_path,
        extra_breaks=args.breakpoint,
        auto_run=args.auto_run,
    )

    executable_path = game_app.install_path / launch_executable_rel
    working_dir = game_app.install_path / working_dir_rel if working_dir_rel else game_app.install_path

    forced_windowed_args = []
    if args.force_windowed:
        forced_windowed_args = [
            "-windowed",
            "-noborder",
        ]

    app_args = app_config_args + forced_windowed_args + args.app_args

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

    return {
        "app_args": app_args,
        "cwd": str(working_dir),
        "env": env_vars,
        "executable_path": executable_path,
        "game_app": game_app,
        "gdb_cmd": gdb_cmd,
        "gdb_script_path": gdb_script_path,
        "log_path": log_path,
        "proton_app": proton_app,
        "working_dir": working_dir,
    }


def print_launch_summary(context, ui_enabled):
    print(f"Proton: {context['proton_app'].name} ({context['proton_app'].appid})")
    print(f"App: {context['game_app'].name} ({context['game_app'].appid})")
    print(f"Using install dir: {context['game_app'].install_path}")
    print(f"Using Proton prefix: {context['game_app'].prefix_path}")
    print("--------------------------------------------")
    print(f"Using working dir: {context['working_dir']}")
    print(f"Using launch executable: {context['executable_path']}")
    print(f"Using arguments: {list_to_space_str(context['app_args'])}")
    print("--------------------------------------------")
    print(f"GDB log file: {context['log_path']}")
    print(f"Debugger UI: {'enabled' if ui_enabled else 'disabled'}")
    print()

    if ui_enabled:
        print("UI hotkeys:")
        print("  r run, c continue, s step, n next")
        print("  i stepi, o nexti, l ilog, b backtrace, t all-thread backtrace")
        print("  d disassemble around $pc, y info symbol $pc, w wine-reload")
        print("  p interrupt, : custom gdb command, q quit")
    else:
        print("Inside GDB:")
        print("  run      -> start the game")
        print("  ilog     -> dump current state to log")
        print("  slog     -> dump state, then step")
        print("  nlog     -> dump state, then next")
        print("  silog    -> dump state, then stepi")
        print("  nilog    -> dump state, then nexti")
        print("  clog     -> dump state, then continue")

    print()
    print("Launching GDB:")
    print(" ", shell_join(context["gdb_cmd"]))
    print()


def launch_plain_gdb(context):
    return subprocess.call(
        context["gdb_cmd"],
        cwd=context["cwd"],
        env=context["env"],
    )


class DebuggerUI(object):
    def __init__(self, context):
        self.context = context
        self.command_buffer = ""
        self.command_history = deque(maxlen=32)
        self.command_mode = False
        self.execution_state = "starting"
        self.last_command = ""
        self.last_status = "Starting GDB..."
        self.log_lines = deque(maxlen=120)
        self.master_fd = None
        self.output_lines = deque(maxlen=1200)
        self.partial_output = ""
        self.proc = None
        self.prompt_ready = False
        self.quit_requested = False
        self.stop_reason = ""
        self.title = (
            f"{context['game_app'].name} ({context['game_app'].appid})"
            f" | {context['proton_app'].name}"
        )
        self._last_log_refresh = 0.0

    def run(self):
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd

        try:
            self.proc = subprocess.Popen(
                self.context["gdb_cmd"],
                cwd=self.context["cwd"],
                env=self.context["env"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        set_nonblocking(self.master_fd)

        try:
            curses.wrapper(self._curses_main)
        finally:
            self._shutdown()

        if self.proc is None:
            return 1

        rc = self.proc.poll()
        return rc if rc is not None else 0

    def _shutdown(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self._send_commands(["quit"], running=False, status="Quitting GDB...")
                deadline = time.time() + 1.5
                while time.time() < deadline and self.proc.poll() is None:
                    self._pump_output()
                    time.sleep(0.05)
            except Exception:
                pass

            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def _curses_main(self, stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(100)

        while True:
            self._pump_output()
            self._refresh_log_lines()
            self._draw(stdscr)

            if self.quit_requested:
                break

            ch = stdscr.getch()
            if ch == -1:
                continue

            if self.command_mode:
                self._handle_command_input(ch)
            else:
                self._handle_hotkey(ch)

    def _pump_output(self):
        if self.master_fd is None:
            return

        while True:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0)
            except (OSError, ValueError):
                return

            if not ready:
                break

            try:
                chunk = os.read(self.master_fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EIO):
                    break
                raise

            if not chunk:
                break

            self._consume_output(chunk.decode("utf-8", "replace"))

        if self.proc is not None and self.proc.poll() is not None:
            self.execution_state = "exited"
            if not self.stop_reason:
                self.stop_reason = f"GDB exited with status {self.proc.returncode}."

    def _consume_output(self, text):
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        combined = self.partial_output + normalized
        lines = combined.split("\n")
        self.partial_output = lines.pop()

        for line in lines:
            self.output_lines.append(line)
            self._update_state_from_line(line)

        if "(gdb)" in self.partial_output:
            self.prompt_ready = True
            if self.execution_state == "running":
                self.execution_state = "stopped"

    def _update_state_from_line(self, line):
        stripped = line.strip()
        if not stripped:
            return

        if stripped == "Continuing." or stripped.startswith("Starting program:"):
            self.execution_state = "running"
            self.last_status = stripped
            self.prompt_ready = False
            return

        if (
            stripped.startswith("Breakpoint ")
            or stripped.startswith("Temporary breakpoint ")
            or stripped.startswith("Catchpoint ")
            or "Program received signal" in stripped
            or " received signal " in stripped
        ):
            self.execution_state = "stopped"
            self.stop_reason = stripped
            self.last_status = stripped
            return

        if "cannot find bounds of current function" in stripped:
            self.execution_state = "stopped"
            self.last_status = "No function bounds at $pc. Use i/o for instruction stepping."
            return

        if (
            stripped.startswith("[Inferior ")
            and ("exited" in stripped or "killed" in stripped)
        ) or stripped.startswith("Program terminated with signal"):
            self.execution_state = "exited"
            self.stop_reason = stripped
            self.last_status = stripped
            return

        if stripped == "(gdb)":
            self.prompt_ready = True
            if self.execution_state == "running":
                self.execution_state = "stopped"
            return

        self.last_status = stripped

    def _refresh_log_lines(self):
        now = time.time()
        if now - self._last_log_refresh < 0.5:
            return

        self._last_log_refresh = now
        self.log_lines = deque(tail_file_lines(self.context["log_path"], max_lines=120), maxlen=120)

    def _send_commands(self, commands, running=False, status=None):
        if self.master_fd is None or not commands:
            return

        payload = "".join(f"{command}\n" for command in commands).encode("utf-8")
        os.write(self.master_fd, payload)
        self.last_command = " ; ".join(commands)
        self.command_history.append(self.last_command)
        self.prompt_ready = False
        if running:
            self.execution_state = "running"
        if status:
            self.last_status = status
        else:
            self.last_status = f"Sent {self.last_command}"

    def _interrupt(self):
        if self.master_fd is None:
            return
        os.write(self.master_fd, b"\x03")
        self.prompt_ready = False
        self.execution_state = "running"
        self.last_status = "Interrupt requested."

    def _handle_hotkey(self, ch):
        if ch in (ord("q"), ord("Q")):
            self.quit_requested = True
            return

        if ch in (3, ord("p"), ord("P")):
            self._interrupt()
            return

        if ch == ord(":"):
            self.command_mode = True
            self.command_buffer = ""
            self.last_status = "Enter a raw GDB command."
            return

        actions = {
            ord("r"): (["run"], True, "Running the target."),
            ord("R"): (["run"], True, "Running the target."),
            ord("c"): (["clog"], True, "Logging state and continuing."),
            ord("C"): (["clog"], True, "Logging state and continuing."),
            ord("s"): (["slog"], True, "Logging state and stepping."),
            ord("S"): (["slog"], True, "Logging state and stepping."),
            ord("n"): (["nlog"], True, "Logging state and stepping over."),
            ord("N"): (["nilog"], True, "Logging state and stepping one instruction over."),
            ord("i"): (["silog"], True, "Logging state and stepping one instruction."),
            ord("I"): (["silog"], True, "Logging state and stepping one instruction."),
            ord("o"): (["nilog"], True, "Logging state and stepping one instruction over."),
            ord("O"): (["nilog"], True, "Logging state and stepping one instruction over."),
            ord("l"): (["ilog"], False, "Dumping the current debugger state."),
            ord("L"): (["ilog"], False, "Dumping the current debugger state."),
            ord("b"): (["bt"], False, "Collecting a backtrace."),
            ord("B"): (["bt"], False, "Collecting a backtrace."),
            ord("t"): (["thread apply all bt full"], False, "Collecting full backtraces for all threads."),
            ord("T"): (["thread apply all bt full"], False, "Collecting full backtraces for all threads."),
            ord("d"): (
                ["info symbol $pc", "x/20i $pc", "disassemble $pc-32, $pc+64"],
                False,
                "Inspecting the current instruction window.",
            ),
            ord("D"): (
                ["info symbol $pc", "x/20i $pc", "disassemble $pc-32, $pc+64"],
                False,
                "Inspecting the current instruction window.",
            ),
            ord("y"): (["info symbol $pc"], False, "Looking up the current symbol."),
            ord("Y"): (["info symbol $pc"], False, "Looking up the current symbol."),
            ord("w"): (["wine-reload"], False, "Reloading Wine symbols."),
            ord("W"): (["wine-reload"], False, "Reloading Wine symbols."),
        }

        action = actions.get(ch)
        if action:
            commands, running, status = action
            self._send_commands(commands, running=running, status=status)

    def _handle_command_input(self, ch):
        if ch in (27,):
            self.command_mode = False
            self.command_buffer = ""
            self.last_status = "Cancelled raw command entry."
            return

        if ch in (10, 13):
            command = self.command_buffer.strip()
            self.command_mode = False
            self.command_buffer = ""
            if command:
                self._send_commands([command], running=False, status=f"Sent raw command: {command}")
            else:
                self.last_status = "No command entered."
            return

        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.command_buffer = self.command_buffer[:-1]
            return

        if 32 <= ch <= 126:
            self.command_buffer += chr(ch)

    def _draw(self, stdscr):
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        header = self.title
        status = (
            f"State: {self.execution_state}"
            f" | Prompt: {'ready' if self.prompt_ready else 'busy'}"
            f" | Log: {self.context['log_path']}"
        )

        try:
            stdscr.addnstr(0, 0, header, max(width - 1, 0), curses.A_BOLD)
            stdscr.addnstr(1, 0, status, max(width - 1, 0))
        except curses.error:
            pass

        footer_height = 3 if self.command_mode else 2
        body_top = 2
        body_height = max(height - body_top - footer_height, 3)

        if width >= 110 and body_height >= 10:
            left_width = max((width * 2) // 3, 40)
            right_width = width - left_width
            right_top_height = max(body_height // 2, 8)
            self._draw_box(
                stdscr,
                body_top,
                0,
                body_height,
                left_width,
                "GDB Output",
                wrap_lines(self._output_snapshot(), left_width - 2, body_height - 2),
            )
            self._draw_box(
                stdscr,
                body_top,
                left_width,
                right_top_height,
                right_width,
                "Controls",
                wrap_lines(self._control_lines(), right_width - 2, right_top_height - 2),
            )
            self._draw_box(
                stdscr,
                body_top + right_top_height,
                left_width,
                body_height - right_top_height,
                right_width,
                "Log Tail",
                wrap_lines(list(self.log_lines), right_width - 2, body_height - right_top_height - 2),
            )
        else:
            output_height = max(body_height - 8, 3)
            details_height = body_height - output_height
            self._draw_box(
                stdscr,
                body_top,
                0,
                output_height,
                width,
                "GDB Output",
                wrap_lines(self._output_snapshot(), width - 2, output_height - 2),
            )
            self._draw_box(
                stdscr,
                body_top + output_height,
                0,
                details_height,
                width,
                "Controls / Log Tail",
                wrap_lines(
                    self._control_lines() + [""] + list(self.log_lines),
                    width - 2,
                    details_height - 2,
                ),
            )

        footer_y = height - footer_height
        help_line = "r run | c continue | s step | n next | i stepi | o nexti | l log | b bt | d disasm | : cmd | q quit"
        try:
            stdscr.addnstr(footer_y, 0, help_line, max(width - 1, 0), curses.A_REVERSE)
        except curses.error:
            pass

        if self.command_mode:
            prompt = f"Command> {self.command_buffer}"
            try:
                stdscr.addnstr(footer_y + 1, 0, prompt, max(width - 1, 0))
                stdscr.addnstr(footer_y + 2, 0, "Enter to send, Esc to cancel.", max(width - 1, 0))
            except curses.error:
                pass
        else:
            try:
                stdscr.addnstr(footer_y + 1, 0, self.last_status, max(width - 1, 0))
            except curses.error:
                pass

        stdscr.refresh()

    def _draw_box(self, stdscr, y, x, height, width, title, lines):
        if height < 3 or width < 4:
            return

        try:
            window = stdscr.derwin(height, width, y, x)
            window.box()
            window.addnstr(0, 2, f" {title} ", max(width - 4, 0))

            inner_height = height - 2
            inner_width = width - 2
            visible = lines[-inner_height:]

            for idx, line in enumerate(visible):
                window.addnstr(idx + 1, 1, line, max(inner_width, 0))
        except curses.error:
            pass

    def _output_snapshot(self):
        lines = list(self.output_lines)
        if self.partial_output:
            lines.append(self.partial_output)
        return lines

    def _control_lines(self):
        stop_reason = self.stop_reason or "-"
        last_command = self.last_command or "-"
        return [
            "r run | c clog | s slog | n nlog",
            "i silog | o nilog | l ilog",
            "b bt | t thread apply all bt full",
            "d inspect $pc | y info symbol $pc",
            "w wine-reload | p interrupt | : raw command | q quit",
            "",
            f"Last command: {last_command}",
            f"Stop reason: {stop_reason}",
        ]


def launch_gdb_ui(context):
    return DebuggerUI(context).run()


def main(args=None):
    parser = argparse.ArgumentParser(
        description="Launch a Steam Play/Proton game under GDB and log debug state to a file.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--auto-run", action="store_true", help="automatically issue 'run' inside gdb")
    parser.add_argument("--breakpoint", "-b", action="append", default=[], help="extra breakpoint to set in gdb")
    parser.add_argument("--force-windowed", action="store_true", default=True, help="add common windowed launch args")
    parser.add_argument("--log-dir", default=".", help="directory for gdb log output")
    ui_group = parser.add_mutually_exclusive_group()
    ui_group.add_argument("--ui", dest="ui", action="store_true", help="launch the built-in debugger UI (default on TTYs)")
    ui_group.add_argument("--no-ui", dest="ui", action="store_false", help="launch plain gdb without the built-in UI")
    parser.add_argument("appid", type=int, nargs="?", default=None)
    parser.add_argument("app_args", nargs=argparse.REMAINDER)
    parser.set_defaults(ui=None)
    args = parser.parse_args(args)

    enable_logging(args.verbose)
    os.environ["DEBUGINFOD_URLS"] = ""
    if args.ui is None:
        args.ui = sys.stdin.isatty() and sys.stdout.isatty()
    elif args.ui and not (sys.stdin.isatty() and sys.stdout.isatty()):
        logger.error("--ui requires an interactive terminal.")
        return 1

    if not args.appid:
        parser.print_help()
        return 1

    context = build_launch_context(args)
    if not context:
        return 1

    print_launch_summary(context, ui_enabled=args.ui)

    try:
        if args.ui:
            return launch_gdb_ui(context)
        return launch_plain_gdb(context)
    finally:
        try:
            context["gdb_script_path"].unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
