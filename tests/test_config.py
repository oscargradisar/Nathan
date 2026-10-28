"""Config tests — no network, no real keys."""

from pathlib import Path

from nathan.config import DEFAULT_CONFIG, load_providers, mask


def test_providers_yaml_loads():
    providers = load_providers(DEFAULT_CONFIG, env_file=None)
    assert {"groq", "gemini", "cerebras", "openrouter", "mistral"} <= providers.keys()
    for p in providers.values():
        assert p.base_url.startswith("https://")
        assert p.key_env.endswith("_API_KEY")
        assert p.models, f"{p.name} has no models"


def test_key_read_from_env(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(
        "providers:\n  demo:\n    base_url: https://example.com/v1\n"
        "    key_env: DEMO_API_KEY\n    models:\n      m1: {}\n"
    )
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    assert load_providers(cfg, env_file=None)["demo"].api_key is None
    monkeypatch.setenv("DEMO_API_KEY", "sk-secret-123")
    assert load_providers(cfg, env_file=None)["demo"].api_key == "sk-secret-123"


def test_mask_hides_secret():
    assert mask("sk-secret-123") == "sk-s…"
    assert mask(None) == "—"
