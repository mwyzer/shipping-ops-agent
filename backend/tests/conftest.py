import pytest

from shipping_agent.sample_data import generate


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Tests are deterministic: never call a real LLM."""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture()
def voyage(tmp_path):
    folder = generate(tmp_path / "data")
    return folder, folder / "master_customers.csv"
