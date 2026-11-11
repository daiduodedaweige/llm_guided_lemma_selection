"""Offline regression checks for proxy-backed live experiments."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evaluation import CacheMode, LemmaEvaluator, VerificationResult
from prompt_llms import Claude, GPT, get_model
from scripts.run_live import load_codex_profile, main


class LiveTests(unittest.TestCase):
    def test_codex_profile_reads_key_file_location_without_running_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "proxy.key"
            key.write_text("offline-test-key\n")
            config = root / "config.toml"
            config.write_text(
                'model = "gpt-6.1-sol"\n'
                'model_provider = "proxy"\n'
                'model_reasoning_effort = "xhigh"\n'
                '[model_providers.proxy]\n'
                'base_url = "https://example.test/v1"\n'
                'wire_api = "responses"\n'
                '[model_providers.proxy.auth]\n'
                'command = "cat"\n'
                f'args = ["{key}"]\n'
            )
            profile = load_codex_profile(config)
            self.assertEqual(profile["model"], "gpt-6.1-sol")
            self.assertEqual(profile["reasoning_effort"], "xhigh")
            self.assertEqual(profile["key_file"], key)
            config.write_text(config.read_text().replace('command = "cat"',
                                                        'command = "sh"'))
            with self.assertRaisesRegex(ValueError, "arbitrary auth commands"):
                load_codex_profile(config)

    def test_responses_api_accepts_single_message_output(self):
        model = GPT("gpt-6.1-sol", "unused", cache={})
        response = SimpleNamespace(
            output_text="<json>{\"lemmas\":[]}</json>",
            output=[SimpleNamespace(type="message")],
            usage=SimpleNamespace(input_tokens=12, output_tokens=7),
        )
        calls = []
        model.client = SimpleNamespace(
            responses=SimpleNamespace(
                create=lambda **kwargs: (calls.append(kwargs), response)[1]
            )
        )
        with mock.patch.dict(os.environ, {
            "LLM_WIRE_API": "responses",
            "LLM_REASONING_EFFORT": "xhigh",
        }):
            result = model._generate_responses("test prompt", num_samples=1)
        self.assertEqual(result[0]["text"], response.output_text)
        self.assertEqual(result[0]["input_tokens"], 12)
        self.assertEqual(calls[0]["reasoning"], {"effort": "xhigh"})

    def test_responses_provider_accepts_future_model_id(self):
        with mock.patch.dict(os.environ, {"LLM_WIRE_API": "responses"}):
            model = get_model("my-next-model", "unused", cache={})
        self.assertIsInstance(model, GPT)
        self.assertEqual(model.model_name, "my-next-model")

    def test_live_proxy_environment_does_not_change_read_only_replay(self):
        with mock.patch.dict(os.environ, {"LLM_WIRE_API": "responses"}):
            model = get_model(
                "us.anthropic.claude-sonnet-4-20250514-v1:0",
                "unused", cache={}, cache_read_only=True,
            )
        self.assertIsInstance(model, Claude)

    def test_ebmc_uses_configured_executable_with_spaces_in_path(self):
        with tempfile.TemporaryDirectory(prefix="lemma ebmc ") as directory:
            root = Path(directory)
            module = root / "module name.sv"
            module.write_text(
                "module main(input logic rst);\n"
                "property prop; rst |-> rst; endproperty\n"
                "endmodule\n"
            )
            args_path = root / "args.json"
            ebmc = root / "fake ebmc"
            ebmc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "open(os.environ['FAKE_EBMC_ARGS'], 'w').write(json.dumps(sys.argv[1:]))\n"
                "print('** Results: PROVED')\n"
            )
            ebmc.chmod(0o755)
            evaluator = LemmaEvaluator(
                str(root), "ebmc", str(module), cache={},
                cache_mode=CacheMode.NO_CACHE, ebmc_path=str(ebmc),
            )
            with mock.patch.dict(os.environ, {"FAKE_EBMC_ARGS": str(args_path)}):
                result = evaluator.run_ebmc(str(module), "one_induction")
            self.assertEqual(result["verification_result"], VerificationResult.PROVEN)
            self.assertEqual(
                json.loads(args_path.read_text()),
                [str(module), "--k-induction", "--bound", "1",
                 "--reset", "rst", "--trace"],
            )

    def test_dry_run_does_not_write_or_disclose_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "proxy.key"
            key.write_text("private-test-secret\n")
            key.chmod(0o600)
            config = root / "config.toml"
            config.write_text(
                'model = "gpt-6.1-sol"\n'
                'model_provider = "proxy"\n'
                '[model_providers.proxy]\n'
                'base_url = "https://example.test/v1"\n'
                'wire_api = "responses"\n'
                '[model_providers.proxy.auth]\n'
                'command = "cat"\n'
                f'args = ["{key}"]\n'
            )
            modules = root / "modules"
            modules.mkdir()
            (modules / "sample.sv").write_text("module main; endmodule\n")
            ebmc = root / "ebmc"
            ebmc.write_text("#!/usr/bin/env python3\nprint('5.9 (ebmc-5.9)')\n")
            ebmc.chmod(0o755)
            output = root / "fresh"
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = main([
                    "--codex-config", str(config),
                    "--pipeline", "agent",
                    "--modules-dir", str(modules),
                    "--output-dir", str(output),
                    "--ebmc-path", str(ebmc),
                    "--dry-run",
                ])
            self.assertEqual(result, 0)
            self.assertFalse(output.exists())
            self.assertNotIn("private-test-secret", stream.getvalue())
            self.assertEqual(json.loads(stream.getvalue())["model_id"], "gpt-6.1-sol")
            self.assertEqual(json.loads(stream.getvalue())["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
