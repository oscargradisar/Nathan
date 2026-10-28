"""Keep the Chrome extension consistent with providers.yaml (no browser needed).

If onboarding.json is stale, this test rewrites it and fails once, so the change shows
up in git and the JS tests run against the current config.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

from nathan.config import DEFAULT_CONFIG, load_providers
from nathan.onboarding.server import Hub

EXT = Path(__file__).resolve().parent.parent / "extension"


def test_extension_config_snapshot_in_sync():
    current = Hub(load_providers(DEFAULT_CONFIG, env_file=None)).onboarding_json()
    path = EXT / "test" / "onboarding.json"
    saved = json.loads(path.read_text()) if path.exists() else None
    if saved != current:
        path.write_text(json.dumps(current, indent=2) + "\n")
    assert saved == current, "extension/test/onboarding.json was stale and has been regenerated"


def test_manifest_permissions_are_narrow():
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    perms = set(manifest.get("permissions", []))
    assert perms <= {"storage"}
    hosts = manifest["host_permissions"]
    assert "<all_urls>" not in hosts and "http://127.0.0.1/*" in hosts
    providers = load_providers(DEFAULT_CONFIG, env_file=None)
    for p in providers.values():
        if p.onboarding and p.onboarding.method == "extension":
            host = urlparse(p.onboarding.key_page).netloc
            assert f"https://{host}/*" in hosts, host
            assert f"https://{host}/*" in manifest["content_scripts"][0]["matches"], host
    assert len(hosts) == 1 + sum(
        1 for p in providers.values() if p.onboarding and p.onboarding.method == "extension")
