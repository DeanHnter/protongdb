
# protongdb
## A small little helper for running games with Proton and debugging with GDB

## Requirements

 - At least Python 3.5
 - [protontricks](https://github.com/Matoking/protontricks) pip package and its dependencies (`pip install --user protontricks`)

## Usage

The basic usage is as follows:

On any Steam app that you have a prefix for (ie. started the game at least once) you can use
```
protongdb <appid> [args...]
```
to start it with a debugger.

On interactive terminals, `protongdb` now launches a built-in terminal UI by default. Use `--no-ui` if you want plain GDB instead.

You can find the appid for the game using the Properties > Updates tab of the game.

If the game crashes or you hit a breakpoint, you can run `wine-reload` to resolve any symbols, etc.

You can then get a backtrace or step-through the code like a native app.

Unlike Steam, environment variables are inherited from your environment, so specify them before `protongdb` (no need for `%command%` stuff).

By default, `protongdb` also installs a Wine/Proton startup skip list in GDB so `step` / `next` are less likely to walk through loader and thread bootstrap glue before you reach game code. Use `--no-default-skips` to disable that, `--skip-function NAME` to add more skipped functions, and `--skip-file GLOB` to add skipped source files or file globs.

## Debugger UI

The built-in UI gives you hotkeys for the common debugger actions so you do not have to keep typing commands:

- `r` run
- `c` continue with logging (`clog`)
- `s` step with logging (`slog`)
- `n` next with logging (`nlog`)
- `i` instruction-step with logging (`silog`)
- `o` instruction-next with logging (`nilog`)
- `l` dump current state (`ilog`)
- `b` backtrace
- `t` `thread apply all bt full`
- `d` inspect the current PC with symbol lookup and disassembly
- `y` `info symbol $pc`
- `k` show active skip rules
- `w` `wine-reload`
- `p` interrupt the inferior
- `:` enter a raw GDB command
- `q` quit the UI

The right-hand pane shows a live tail of the GDB log file.

## Instruction Stepping

If GDB reports:

```text
cannot find bounds of current function
```

source-level `step` / `next` are no longer reliable. Use instruction-level stepping instead:

- `silog` to dump state and run `stepi`
- `nilog` to dump state and run `nexti`

The built-in UI exposes those as `i` and `o`.

## Skip Rules

The default startup skip list targets common loader and thread-entry wrappers such as `_start`, `wld_start`, `__libc_start_main`, `__wine_main`, `__wine_spec_exe_entry`, `start_thread`, `BaseThreadInitThunk`, and `RtlUserThreadStart`.

This only changes stepping behavior. It helps `step` / `next` avoid descending into Wine/Proton glue, but it does not prevent that code from running and it does not suppress normal breakpoint or catchpoint stops.

Use `skips` or `info skip` inside GDB, or press `k` in the UI, to inspect the active rules.

## Special Thanks

Thanks to the creators of [protontricks](https://github.com/Matoking/protontricks) as it had a lot of useful helper functions to make this work, and [Rémi Bernon](https://github.com/rbernon) for the WineReload code.
