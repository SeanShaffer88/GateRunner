# Changelog

## 0.1.0 — 2026-10-24

- Initial release.
- `gaterunner.py run [gate.toml] [--dry-run]`: ordered step execution via subprocess, per-step prefixed streaming output, progress % + mean-based ETA before each step, stop on first required failure, optional steps warn and continue, per-step timeouts (`TIMEOUT`), and a final PASS/FAIL/SKIP/TIMEOUT summary table with durations. Exit 1 on required failure.
- `gate.toml` format parsed with `tomllib`: `[gate] name` plus `[[step]]` entries with argv-list or shell-string `command`, `required`, `timeout_sec`, `env`, and relative `cwd`.
- Warnings engine: filtered `cargo test` runs (InstallScope PR #11 lesson), `cargo test --lib` skipping binary/GUI tests, and `dart format` ordered before `flutter pub get`.
- `gaterunner.py init --stack rust|rust-gui|python|dotnet|flutter`: correct-by-construction gate presets — full unfiltered `cargo test --locked`, windows-msvc clippy as an optional step, and the Flutter pub-get → format → analyze → test order.
- Test suite: 21 unittests covering execution, failure/short-circuit, optional steps, timeouts, all warnings, ETA formatting, and every `init` preset.
