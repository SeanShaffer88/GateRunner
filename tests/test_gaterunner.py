import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gaterunner as gr


def write_gate(dirpath: Path, body: str) -> Path:
    p = dirpath / "gate.toml"
    p.write_text(body, encoding="utf-8")
    return p


def py_step(name, code, **extra):
    lines = [f'[[step]]', f'name = "{name}"', f'command = [{sys.executable!r}, "-c", {code!r}]'.replace("'", '"')]
    # Build TOML manually with proper escaping via repr-like double quotes:
    # sys.executable and code contain no double quotes in our test cases.
    cmd = f'command = ["{sys.executable}", "-c", "{code}"]'
    lines = [f'[[step]]', f'name = "{name}"', cmd]
    for k, v in extra.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        else:
            lines.append(f"{k} = {v}")
    return "\n".join(lines)


class RunTests(unittest.TestCase):
    def test_three_passing_steps_exit_0(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = '[gate]\nname = "ok"\n' + "\n".join(
                py_step(f"s{i}", "print(1)") for i in range(3))
            gate = gr.load_gate(write_gate(d, body))
            out = io.StringIO()
            code = gr.run_gate(gate, out=out)
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("Summary:", text)
            self.assertEqual(text.count("PASS"), 3)
            self.assertIn("[step 1/3", text)

    def test_required_failure_stops_later_steps(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            marker = "marker.txt"
            body = '[gate]\nname = "fail"\n' + "\n".join([
                py_step("boom", "import sys; sys.exit(3)"),
                py_step("later", "open('marker.txt','w').write('x')"),
            ])
            gate = gr.load_gate(write_gate(d, body))
            code = gr.run_gate(gate, out=io.StringIO())
            self.assertEqual(code, 1)
            self.assertFalse((d / marker).exists())

    def test_optional_failure_continues(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = '[gate]\nname = "opt"\n' + "\n".join([
                py_step("optfail", "import sys; sys.exit(2)", required=False),
                py_step("after", "open('after.txt','w').write('x')"),
            ])
            gate = gr.load_gate(write_gate(d, body))
            code = gr.run_gate(gate, out=io.StringIO())
            self.assertEqual(code, 0)
            self.assertTrue((d / "after.txt").exists())

    def test_timeout_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = '[gate]\nname = "slow"\n' + py_step(
                "sleep", "import time; time.sleep(30)", timeout_sec=1)
            gate = gr.load_gate(write_gate(d, body))
            out = io.StringIO()
            code = gr.run_gate(gate, out=out)
            self.assertEqual(code, 1)
            self.assertIn("TIMEOUT", out.getvalue())

    def test_string_command_and_env_and_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "sub").mkdir()
            body = (
                '[gate]\nname = "misc"\n[[step]]\nname = "shell"\n'
                f'command = "{sys.executable} -c \\"open(\'x.txt\',\'w\').write(\'y\')\\""\n'
                'cwd = "sub"\n'
            )
            gate = gr.load_gate(write_gate(d, body))
            code = gr.run_gate(gate, out=io.StringIO())
            self.assertEqual(code, 0)
            self.assertTrue((d / "sub" / "x.txt").exists())

    def test_dry_run_executes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            body = '[gate]\nname = "dry"\n' + py_step("mk", "open('nope.txt','w').write('x')")
            gate = gr.load_gate(write_gate(d, body))
            out = io.StringIO()
            code = gr.run_gate(gate, dry_run=True, out=out)
            self.assertEqual(code, 0)
            self.assertFalse((d / "nope.txt").exists())
            self.assertIn("Dry run", out.getvalue())


class WarningTests(unittest.TestCase):
    def gate_from_body(self, body):
        with tempfile.TemporaryDirectory() as td:
            return gr.load_gate(write_gate(Path(td), body))

    def test_filtered_cargo_test_detected(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n[[step]]\nname="t"\ncommand = ["cargo", "test", "--lib", "settings"]\n')
        ws = gr.collect_warnings(g)
        self.assertTrue(any("filtered test run" in w and "PR #11" in w for w in ws), ws)

    def test_positional_filter_detected(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n[[step]]\nname="t"\ncommand = "cargo test my_test"\n')
        self.assertTrue(any("filtered" in w for w in gr.collect_warnings(g)))

    def test_lib_warning(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n[[step]]\nname="t"\ncommand = ["cargo", "test", "--lib", "--locked"]\n')
        ws = gr.collect_warnings(g)
        self.assertTrue(any("--lib" in w and "skipped" in w for w in ws), ws)
        # --lib alone (no positional) is not a filtered run
        self.assertFalse(any("filtered" in w for w in ws), ws)

    def test_full_cargo_test_no_warning(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n[[step]]\nname="t"\ncommand = ["cargo", "test", "--locked"]\n')
        self.assertEqual(gr.collect_warnings(g), [])

    def test_flutter_ordering_warning(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n'
            '[[step]]\nname="fmt"\ncommand = ["dart", "format", "."]\n'
            '[[step]]\nname="pub"\ncommand = ["flutter", "pub", "get"]\n')
        ws = gr.collect_warnings(g)
        self.assertTrue(any("pub get" in w and "BEFORE" in w for w in ws), ws)

    def test_flutter_correct_order_no_warning(self):
        g = self.gate_from_body(
            '[gate]\nname="x"\n'
            '[[step]]\nname="pub"\ncommand = ["flutter", "pub", "get"]\n'
            '[[step]]\nname="fmt"\ncommand = ["dart", "format", "--set-exit-if-changed", "."]\n')
        self.assertEqual(gr.collect_warnings(g), [])


class EtaTests(unittest.TestCase):
    def test_unknown_before_any_complete(self):
        self.assertEqual(gr.format_eta([], 3), "unknown")

    def test_mean_based(self):
        self.assertEqual(gr.format_eta([60.0, 60.0], 1), "~1m")
        self.assertEqual(gr.format_eta([10.0], 3), "~30s")
        self.assertEqual(gr.format_eta([120.0], 1), "~2m")

    def test_zero_remaining(self):
        self.assertEqual(gr.format_eta([10.0], 0), "~0s")

    def test_hours(self):
        self.assertEqual(gr.format_eta([3600.0], 2), "~2h")


class InitTests(unittest.TestCase):
    def test_every_stack_parseable(self):
        for stack in ("rust", "rust-gui", "python", "dotnet", "flutter"):
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "gate.toml"
                code = gr.main(["init", "--stack", stack, "--out", str(out)])
                self.assertEqual(code, 0, stack)
                gate = gr.load_gate(out)
                self.assertTrue(gate.steps, stack)

    def test_rust_has_no_test_filter(self):
        for stack in ("rust", "rust-gui"):
            text = gr.generate_gate(stack)
            gate_text = text
            self.assertIn('["cargo", "test", "--locked"]', gate_text)
            self.assertNotIn('"--lib"', gate_text)
            # windows clippy step is optional
            self.assertIn("required = false", gate_text)

    def test_rust_gui_comment_emphasises_no_lib(self):
        self.assertIn("WITHOUT --lib", gr.generate_gate("rust-gui"))

    def test_flutter_preset_order(self):
        gate_steps = None
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "gate.toml"
            gr.main(["init", "--stack", "flutter", "--out", str(out)])
            gate_steps = [s.name for s in gr.load_gate(out).steps]
        self.assertEqual(gate_steps, ["pub-get", "format", "analyze", "test"])

    def test_init_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "gate.toml"
            out.write_text("x", encoding="utf-8")
            code = gr.main(["init", "--stack", "rust", "--out", str(out)])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
