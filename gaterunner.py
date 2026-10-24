#!/usr/bin/env python3
"""GateRunner — a unified local quality-gate runner.

Python 3.11+ (uses tomllib), standard library only, zero dependencies.

A gate is declared in a gate.toml file:

    [gate]
    name = "my-project"

    [[step]]
    name = "tests"
    command = ["cargo", "test", "--locked"]
    required = true          # default
    timeout_sec = 600        # optional
    # env = { FOO = "1" }    # optional extra environment variables
    # cwd = "sub/dir"        # optional, relative to the gate file

Commands may be an argv list (preferred, safer — no shell involved) or a
single string, which is run through the shell. String commands come from
the user's own gate.toml; that file is trusted configuration (see README).

Lessons encoded as warnings (see collect_warnings):
  * filtered `cargo test` runs can miss failures (InstallScope PR #11);
  * `cargo test --lib` skips binary-target tests, where GUI tests live;
  * `dart format` before `flutter pub get` rewrites files in an old style.
"""
from __future__ import annotations

import argparse
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    command: list[str] | str
    required: bool = True
    timeout_sec: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    def command_display(self) -> str:
        if isinstance(self.command, list):
            return " ".join(shlex.quote(str(c)) for c in self.command)
        return self.command


@dataclass
class Gate:
    name: str
    steps: list[Step]
    path: Path | None = None


@dataclass
class StepResult:
    name: str
    result: str  # PASS | FAIL | SKIP | TIMEOUT
    duration: float = 0.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_gate(path: str | Path) -> Gate:
    p = Path(path)
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    gate_info = data.get("gate", {})
    name = str(gate_info.get("name", p.stem))
    steps: list[Step] = []
    for raw in data.get("step", []):
        cmd = raw.get("command")
        if cmd is None:
            raise ValueError(f"step {raw.get('name', '?')!r} has no command")
        if isinstance(cmd, list):
            command: list[str] | str = [str(c) for c in cmd]
        else:
            command = str(cmd)
        timeout = raw.get("timeout_sec")
        steps.append(
            Step(
                name=str(raw.get("name", command if isinstance(command, str) else " ".join(command))),
                command=command,
                required=bool(raw.get("required", True)),
                timeout_sec=float(timeout) if timeout is not None else None,
                env={str(k): str(v) for k, v in dict(raw.get("env", {})).items()},
                cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            )
        )
    return Gate(name=name, steps=steps, path=p)


# ---------------------------------------------------------------------------
# ETA (pure)
# ---------------------------------------------------------------------------


def format_eta(completed_durations: list[float], remaining_count: int) -> str:
    """Mean-based ETA string.

    Pure function. Returns "unknown" when nothing has completed yet (or
    nothing remains to estimate against a mean), otherwise a rounded
    "~Ns" / "~Nm" / "~Nm Ns" / "~Nh Nm" string for mean * remaining.
    """
    if not completed_durations or remaining_count <= 0:
        if remaining_count <= 0 and completed_durations:
            return "~0s"
        return "unknown"
    mean = sum(completed_durations) / len(completed_durations)
    total = int(round(mean * remaining_count))
    if total < 60:
        return f"~{total}s"
    if total < 3600:
        minutes, seconds = divmod(total, 60)
        return f"~{minutes}m" if seconds == 0 else f"~{minutes}m {seconds}s"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"~{hours}h" if minutes == 0 else f"~{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Warnings engine
# ---------------------------------------------------------------------------

# Flags for `cargo test` that consume the following token as their value.
_CARGO_VALUE_FLAGS = {
    "--target", "--features", "--package", "-p", "--manifest-path",
    "--color", "--jobs", "-j", "--profile", "--target-dir",
}


def _command_tokens(command: list[str] | str) -> list[str]:
    if isinstance(command, list):
        return [str(c) for c in command]
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _cargo_test_warnings(step: Step) -> list[str]:
    tokens = _command_tokens(step.command)
    warnings: list[str] = []
    # Find a `cargo ... test` invocation.
    try:
        cargo_idx = tokens.index("cargo")
    except ValueError:
        return warnings
    rest = tokens[cargo_idx + 1:]
    if "test" not in rest:
        return warnings
    after = rest[rest.index("test") + 1:]

    # Collect positional (filter) arguments: non-flag tokens that are not a
    # value for a preceding value-flag, stopping at `--`.
    positional: list[str] = []
    i = 0
    while i < len(after):
        tok = after[i]
        if tok == "--":
            break
        if tok in _CARGO_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positional.append(tok)
        i += 1

    if positional:
        warnings.append(
            f"step '{step.name}': filtered test run — full suite required "
            f"(PR #11 lesson): `cargo test` has filter argument(s) "
            f"{positional}; a filtered run once missed a golden-fixture failure"
        )
    if "--lib" in after:
        warnings.append(
            f"step '{step.name}': `cargo test --lib` runs library tests only — "
            "GUI/binary-target tests may be skipped; prefer a full "
            "`cargo test --locked` without --lib"
        )
    return warnings


def _flutter_warnings(steps: list[Step]) -> list[str]:
    warnings: list[str] = []
    first_format: int | None = None
    first_pubget: int | None = None
    format_step_name = ""
    for idx, step in enumerate(steps):
        disp = step.command_display()
        if "dart format" in disp and first_format is None:
            first_format = idx
            format_step_name = step.name
        if "flutter pub get" in disp and first_pubget is None:
            first_pubget = idx
    if first_format is not None and (first_pubget is None or first_format < first_pubget):
        warnings.append(
            f"step '{format_step_name}': `dart format` is ordered before any "
            "`flutter pub get` step — run `flutter pub get` BEFORE "
            "`dart format`, or the formatter rewrites files in an old style "
            "(Flutter ordering lesson)"
        )
    return warnings


def collect_warnings(gate: Gate) -> list[str]:
    warnings: list[str] = []
    for step in gate.steps:
        warnings.extend(_cargo_test_warnings(step))
    warnings.extend(_flutter_warnings(gate.steps))
    return warnings


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _run_one(step: Step, base_dir: Path) -> tuple[str, float]:
    """Run one step, streaming output prefixed with the step name.

    Returns (result, duration) where result is PASS / FAIL / TIMEOUT.
    """
    env = dict(os.environ)
    env.update(step.env)
    cwd = (base_dir / step.cwd) if step.cwd else base_dir
    kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(cwd),
    )
    if isinstance(step.command, list):
        proc = subprocess.Popen([str(c) for c in step.command], **kwargs)
    else:
        proc = subprocess.Popen(step.command, shell=True, **kwargs)

    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    start = time.monotonic()
    timed_out = False
    done_reading = False
    while True:
        if step.timeout_sec is not None and (time.monotonic() - start) > step.timeout_sec:
            timed_out = True
            proc.kill()
        try:
            line = lines.get(timeout=0.05)
        except queue.Empty:
            if proc.poll() is not None and done_reading:
                break
            if proc.poll() is not None and lines.empty():
                # Reader may still be flushing; give it a brief chance.
                continue
            continue
        if line is None:
            done_reading = True
            if proc.poll() is not None:
                break
            continue
        sys.stdout.write(f"[{step.name}] {line}")
        sys.stdout.flush()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except Exception:
            pass
    duration = time.monotonic() - start
    if timed_out:
        return "TIMEOUT", duration
    return ("PASS" if proc.returncode == 0 else "FAIL"), duration


def run_gate(gate: Gate, dry_run: bool = False, out=None) -> int:
    out = out or sys.stdout
    warnings = collect_warnings(gate)
    total = len(gate.steps)
    print(f"Gate: {gate.name} ({total} step{'s' if total != 1 else ''})", file=out)
    for w in warnings:
        print(f"WARNING: {w}", file=out)

    if dry_run:
        print("Dry run — plan only, nothing will be executed:", file=out)
        for i, step in enumerate(gate.steps, start=1):
            pct = round((i - 1) / total * 100) if total else 100
            print(
                f"[step {i}/{total} — {pct}% — ETA unknown] "
                f"{step.name}: {step.command_display()}"
                f"{'' if step.required else ' (optional)'}",
                file=out,
            )
        return 0

    base_dir = gate.path.parent if gate.path else Path.cwd()
    results: list[StepResult] = []
    durations: list[float] = []
    failed_required = False

    for i, step in enumerate(gate.steps, start=1):
        if failed_required:
            results.append(StepResult(step.name, "SKIP", 0.0))
            continue
        pct = round((i - 1) / total * 100) if total else 100
        eta = format_eta(durations, total - (i - 1))
        eta_text = "ETA unknown" if eta == "unknown" else f"ETA {eta} based on completed steps"
        print(f"[step {i}/{total} — {pct}% — {eta_text}] {step.name}: {step.command_display()}", file=out, flush=True)
        result, duration = _run_one(step, base_dir)
        durations.append(duration)
        results.append(StepResult(step.name, result, duration))
        if result != "PASS":
            if step.required:
                print(f"Step '{step.name}' {result} (required) — stopping gate.", file=out)
                failed_required = True
            else:
                print(f"Step '{step.name}' {result} (optional) — continuing.", file=out)

    print("\nSummary:", file=out)
    print(f"{'Step':<30} {'Result':<8} Duration", file=out)
    print(f"{'-' * 30} {'-' * 8} --------", file=out)
    for r in results:
        print(f"{r.name:<30} {r.result:<8} {r.duration:.2f}s", file=out)
    return 1 if failed_required else 0


# ---------------------------------------------------------------------------
# init presets — correct by construction
# ---------------------------------------------------------------------------

_RUST_CLIPPY_WINDOWS_COMMENT = (
    "# The windows-msvc clippy step is required=false: the target may be\n"
    "# absent on a non-Windows / unprepared machine. Install it with\n"
    "# `rustup target add x86_64-pc-windows-msvc` and flip required to true\n"
    "# when every gate machine has it.\n"
)

PRESETS: dict[str, str] = {
    "rust": """\
[gate]
name = "rust-gate"

[[step]]
name = "fmt"
command = ["cargo", "fmt", "--all", "--", "--check"]

[[step]]
name = "clippy-host"
command = ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]

{windows_comment}[[step]]
name = "clippy-windows-msvc"
command = ["cargo", "clippy", "--target", "x86_64-pc-windows-msvc", "--all-targets", "--", "-D", "warnings"]
required = false

[[step]]
name = "test"
# FULL suite, no filter argument — a filtered run once missed a
# golden-fixture failure (InstallScope PR #11 lesson).
command = ["cargo", "test", "--locked"]
""",
    "rust-gui": """\
[gate]
name = "rust-gui-gate"

[[step]]
name = "fmt"
command = ["cargo", "fmt", "--all", "--", "--check"]

[[step]]
name = "clippy-host"
command = ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]

{windows_comment}[[step]]
name = "clippy-windows-msvc"
command = ["cargo", "clippy", "--target", "x86_64-pc-windows-msvc", "--all-targets", "--", "-D", "warnings"]
required = false

[[step]]
name = "test"
# GUI projects: tests live in the BINARY target as well as the library.
# This step is `cargo test --locked` WITHOUT --lib on purpose — a --lib
# gate silently skips all binary-target (GUI) tests. FULL suite, no filter.
command = ["cargo", "test", "--locked"]
""",
    "python": """\
[gate]
name = "python-gate"

[[step]]
name = "compileall"
command = ["python", "-m", "compileall", "."]

[[step]]
name = "test"
command = ["python", "-m", "unittest", "discover"]
""",
    "dotnet": """\
[gate]
name = "dotnet-gate"

[[step]]
name = "build"
command = ["dotnet", "build"]

[[step]]
name = "test"
command = ["dotnet", "test"]
""",
    "flutter": """\
[gate]
name = "flutter-gate"

# Order matters: `flutter pub get` MUST come before `dart format`, or the
# formatter falls back to an old style and rewrites files.
[[step]]
name = "pub-get"
command = ["flutter", "pub", "get"]

[[step]]
name = "format"
command = ["dart", "format", "--set-exit-if-changed", "."]

[[step]]
name = "analyze"
command = ["flutter", "analyze"]

[[step]]
name = "test"
command = ["flutter", "test"]
""",
}


def generate_gate(stack: str) -> str:
    if stack not in PRESETS:
        raise ValueError(f"unknown stack {stack!r}; choose from {sorted(PRESETS)}")
    return PRESETS[stack].replace("{windows_comment}", _RUST_CLIPPY_WINDOWS_COMMENT)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gaterunner", description=__doc__)
    parser.add_argument("--version", action="version", version=f"gaterunner {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run a gate")
    p_run.add_argument("gate", nargs="?", default="gate.toml")
    p_run.add_argument("--dry-run", action="store_true")

    p_init = sub.add_parser("init", help="generate a gate.toml for a stack")
    p_init.add_argument("--stack", required=True, choices=sorted(PRESETS))
    p_init.add_argument("--out", default="gate.toml")
    p_init.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        out_path = Path(args.out)
        if out_path.exists() and not args.force:
            print(f"error: {out_path} exists (use --force to overwrite)", file=sys.stderr)
            return 2
        out_path.write_text(generate_gate(args.stack), encoding="utf-8", newline="\n")
        print(f"Wrote {out_path} for stack '{args.stack}'.")
        return 0

    if args.cmd == "run":
        try:
            gate = load_gate(args.gate)
        except FileNotFoundError:
            print(f"error: gate file not found: {args.gate}", file=sys.stderr)
            return 2
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            print(f"error: cannot parse {args.gate}: {exc}", file=sys.stderr)
            return 2
        return run_gate(gate, dry_run=args.dry_run)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
