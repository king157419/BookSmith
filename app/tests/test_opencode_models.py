import json
import os
from pathlib import Path

import pytest

from app.pipelines.base import Ctx
from app.pipelines.real.common import mask
from app.pipelines.real.opencode_runner import OpenCodeRunner, RunResult


@pytest.mark.parametrize(("model", "base_url", "provider_key"), [
    ("", "", "deepseek-test-key"),
    ("anthropic/test-model", "", "native-test-key"),
    ("deepseek/test-model", "http://localhost:8000/v1", "proxy-test-key"),
    ("custom/team/model", "http://localhost:8000/v1", "gateway-test-key"),
    ("custom/local", "http://localhost:8000/v1", ""),
])
def test_model_credentials(tmp_path, monkeypatch, model, base_url, provider_key):
    monkeypatch.setenv("BOOKSMITH_MODEL", model)
    monkeypatch.setenv("BOOKSMITH_MODEL_BASE_URL", base_url)
    monkeypatch.setenv("BOOKSMITH_MODEL_API_KEY", provider_key)
    monkeypatch.delenv("BOOKSMITH_OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("BOOKSMITH_OPENCODE_HOME", str(tmp_path / "home"))
    ctx = Ctx(task={}, workdir=tmp_path, out_dir=tmp_path, log=lambda *_: None, cancelled=lambda: False)
    runner = OpenCodeRunner(ctx)
    key_files = []

    def inspect_call(prompt, title, files, step, env):
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        selected = model or "deepseek/deepseek-flash"
        assert config["model"] == config["small_model"] == runner.model == selected
        provider_id, model_id = selected.split("/", 1)
        assert list(config["provider"]) == [provider_id]
        provider = config["provider"][provider_id]
        assert runner.use_deepseek_balance == (provider_id == "deepseek" and not base_url)
        if base_url:
            assert provider["options"]["baseURL"] == base_url
        if provider_id == "custom":
            assert provider["npm"] == "@ai-sdk/openai-compatible"
            assert model_id in provider["models"]
        if provider_key:
            ref = provider["options"]["apiKey"]
            assert ref.startswith("{file:") and ref.endswith("}")
            path = Path(ref[6:-1])
            key_files.append(path)
            assert path.read_text(encoding="utf-8") == provider_key
            assert provider_key not in json.dumps(env)
            assert provider_key not in mask(f"Bearer {provider_key}")
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
        else:
            assert "apiKey" not in provider["options"]
        return RunResult(ok=True)

    monkeypatch.setattr(runner, "_run_once", inspect_call)
    assert runner.run("hello", title="test").ok
    if provider_key:
        assert all(not path.exists() for path in key_files)


def test_custom_endpoint_requires_an_explicit_model(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKSMITH_MODEL", "")
    monkeypatch.setenv("BOOKSMITH_MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("BOOKSMITH_MODEL_API_KEY", "other-provider-test-key")
    ctx = Ctx(task={}, workdir=tmp_path, out_dir=tmp_path, log=lambda *_: None, cancelled=lambda: False)
    with pytest.raises(ValueError, match="BOOKSMITH_MODEL="):
        OpenCodeRunner(ctx)
