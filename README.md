## TL;DR

GateRunner is a unified local quality-gate runner: one `gate.toml` declares the exact steps a project must pass, GateRunner runs them in order, streams their output, warns about known-dangerous gate patterns learned from real incidents, and prints a pass/fail summary with timing and a running ETA. Python 3.11+, standard library only, zero dependencies.

```sh
python gaterunner.py init --stack rust      # write a correct-by-construction gate.toml
python gaterunner.py run                    # run gate.toml
python gaterunner.py run gate.toml --dry-run  # print plan + warnings, execute nothing
```

## Why

These lessons are encoded on purpose:

- A gate once ran a **filtered** test subset and missed a golden-fixture failure (InstallScope PR #11) — gates must run the full suite.
- In GUI projects the tests live in the **binary target**, so a `cargo test --lib` gate silently skips them.
- Cfg-gated code sometimes needs an extra pass with a cfg forced on (the OCRKit lesson) — declare it as an explicit step instead of relying on memory.
- Flutter projects must run `flutter pub get` **before** `dart format`, or the formatter falls back to an old style and rewrites files.

## gate.toml spec

```toml
[gate]
name = "my-project"          # optional; defaults to the file stem

[[step]]
name = "tests"               # shown in output and the summary
command = ["cargo", "test", "--locked"]   # argv list (preferred) …
# command = "cargo test --locked"         # … or one shell string
required = true              # optional, default true
timeout_sec = 600            # optional; step is killed and marked TIMEOUT
env = { RUST_BACKTRACE = "1" }  # optional extra environment variables
cwd = "sub/dir"              # optional, relative to the gate file
```

- `command` as an **argv list** is preferred: no shell is involved, so quoting and injection surprises disappear.
- `command` as a **string** is run through the system shell, for pipelines and redirects.
- On a required step's failure (or timeout) the gate stops; remaining steps are reported as `SKIP`. Optional (`required = false`) failures warn and the gate continues.
- Exit code is `1` if any required step failed or timed out, otherwise `0` (and `2` for usage / parse errors).

### Full example

```toml
[gate]
name = "widget-app"

[[step]]
name = "fmt"
command = ["cargo", "fmt", "--all", "--", "--check"]

[[step]]
name = "clippy"
command = ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]
timeout_sec = 600

[[step]]
name = "clippy-windows"
command = ["cargo", "clippy", "--target", "x86_64-pc-windows-msvc", "--all-targets", "--", "-D", "warnings"]
required = false

[[step]]
name = "test"
command = ["cargo", "test", "--locked"]
env = { GOLDEN_BLESS = "0" }
```

## Running a gate

Before each step GateRunner prints progress and an estimate:

```
[step 3/8 — 25% — ETA ~2m based on completed steps] test: cargo test --locked
```

The ETA is the mean duration of completed steps multiplied by the steps remaining; before any step completes it prints `ETA unknown`. Step output is streamed prefixed with the step name (`[test] …`). At the end:

```
Summary:
Step                           Result   Duration
------------------------------ -------- --------
fmt                            PASS     0.42s
test                           FAIL     12.03s
package                        SKIP     0.00s
```

`--dry-run` prints the numbered plan and all warnings without executing anything — the fastest way to audit a gate file.

## Warnings

Warnings are printed on both `run` and `run --dry-run`; they never change the exit code by themselves.

| Pattern | Warning | Lesson |
|---|---|---|
| `cargo test` with a positional filter, e.g. `cargo test --lib settings` or `cargo test my_test` | filtered test run — full suite required | InstallScope PR #11: a filtered gate missed a golden-fixture failure |
| `cargo test --lib` (even unfiltered) | library tests only — GUI/binary-target tests may be skipped | GUI tests live in the binary target; `--lib`-only gates silently skip them |
| A `dart format` step ordered before any `flutter pub get` step | run `flutter pub get` BEFORE `dart format` | Without `pub get` first, the formatter rewrites files in an old style |

## Presets (`init`)

| Stack | Steps, in order |
|---|---|
| `rust` | `cargo fmt --all -- --check` · clippy (host, `-D warnings`) · clippy windows-msvc target (`required = false`, target may be absent) · `cargo test --locked` — full, no filter |
| `rust-gui` | Same as `rust`, with the test step emphasised in comments: `cargo test --locked` **without** `--lib`, because GUI tests live in the binary target |
| `python` | `python -m compileall .` · `python -m unittest discover` |
| `dotnet` | `dotnet build` · `dotnet test` |
| `flutter` | `flutter pub get` · `dart format --set-exit-if-changed .` · `flutter analyze` · `flutter test` — in exactly that order |

```sh
python gaterunner.py init --stack flutter --out gate.toml [--force]
```

`init` refuses to overwrite an existing file without `--force`.

## This project's own gate

The `gate.toml` in this repo runs `compileall` plus the full `unittest` discovery suite — GateRunner gating itself.

## Limitations

- **ETA is a mean-based estimate.** It assumes remaining steps take about as long as completed ones; a gate whose last step is ten times slower than the rest will have a poor ETA until that step has run once.
- **Shell-string commands are trusted configuration.** A `command` given as a string is executed by the shell with your permissions. That is the point of a gate file you wrote yourself — but do not run a `gate.toml` from an untrusted source without reading it first. Prefer argv lists in shared gates.
- Step output is expected to be line-oriented text; a step that floods output without newlines may appear in chunks.
- GateRunner runs steps on the machine it is invoked on; a step whose toolchain is absent fails like it would in a terminal (the rust preset makes the windows-msvc clippy step optional for exactly this reason).
- Requires Python 3.11+ for `tomllib`.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Sean Shaffer.
