from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sdk_development_launcher_forces_loopback_api_bind():
    sdk_launcher = (ROOT / "scripts/sdk-dev.sh").read_text()
    dev_launcher = (ROOT / "scripts/dev.sh").read_text()

    assert 'export CONTEXTGRAPH_API_HOST=127.0.0.1' in sdk_launcher
    assert 'exec "$PROJECT_ROOT/scripts/dev.sh"' in sdk_launcher
    assert 'API_HOST="${CONTEXTGRAPH_API_HOST:-0.0.0.0}"' in dev_launcher
    assert 'uvicorn backend.app:app --host "$API_HOST" --port "$API_PORT"' in dev_launcher
