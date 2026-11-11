"""The candidate generator must not silently omit modules on provider outages."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from get_lemmas import process_prompts  # noqa: E402


class ProviderUnavailable(Exception):
    status_code = 503


class CandidateGenerationTests(unittest.TestCase):
    def test_provider_503_aborts_instead_of_skipping_module(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "prompt.json").write_text(json.dumps({
                "module_name": "example", "model_name": "test",
                "representation": "sva",
            }))
            with mock.patch("get_lemmas.open_cache"), mock.patch(
                "get_lemmas.prompt_llms", side_effect=ProviderUnavailable("503")
            ):
                with self.assertRaisesRegex(RuntimeError, "aborting instead of skipping"):
                    process_prompts(root, prompts, False,
                                    json_output_path=root / "out.json",
                                    csv_output_path=root / "out.csv")
            self.assertFalse((root / "out.json").exists())

    def test_connection_error_aborts_live_gls_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "prompt.json").write_text(json.dumps({
                "module_name": "example", "model_name": "test",
                "representation": "sva",
            }))
            with mock.patch.dict(os.environ, {"LLM_GLS_ENABLED": "1"}), mock.patch(
                "get_lemmas.open_cache"
            ), mock.patch(
                "get_lemmas.prompt_llms", side_effect=ConnectionError("Connection error")
            ):
                with self.assertRaisesRegex(RuntimeError, "aborting instead of skipping"):
                    process_prompts(root, prompts, False,
                                    json_output_path=root / "out.json",
                                    csv_output_path=root / "out.csv")
            self.assertFalse((root / "out.json").exists())


if __name__ == "__main__":
    unittest.main()
