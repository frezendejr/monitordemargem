import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_supabase


class _RespostaFake:
    def __init__(self, dados):
        self._dados = dados

    def raise_for_status(self):
        pass

    def json(self):
        return self._dados


def test_ja_processado_true_quando_existe_no_supabase(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr(
        "storage_supabase.requests.get", lambda *a, **k: _RespostaFake([{"numero_pedido": "1001"}])
    )

    assert storage_supabase.ja_processado("conta_x", "1001") is True


def test_ja_processado_false_quando_vazio(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr("storage_supabase.requests.get", lambda *a, **k: _RespostaFake([]))

    assert storage_supabase.ja_processado("conta_x", "9999") is False


def test_obter_checkpoint_none_quando_nao_existe(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr("storage_supabase.requests.get", lambda *a, **k: _RespostaFake([]))

    assert storage_supabase.obter_checkpoint("conta_x") is None


def test_obter_checkpoint_retorna_valor_salvo(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr(
        "storage_supabase.requests.get",
        lambda *a, **k: _RespostaFake([{"ultima_busca_iso": "2026-09-18T00:00:00+00:00"}]),
    )

    assert storage_supabase.obter_checkpoint("conta_x") == "2026-09-18T00:00:00+00:00"


def test_salvar_checkpoint_chama_post_com_upsert(monkeypatch):
    chamadas = []

    def _post_fake(url, json, headers, timeout):
        chamadas.append((url, json, headers))
        return _RespostaFake(None)

    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr("storage_supabase.requests.post", _post_fake)

    storage_supabase.salvar_checkpoint("conta_x", "2026-09-18T12:00:00+00:00")

    assert len(chamadas) == 1
    url, payload, headers = chamadas[0]
    assert "on_conflict=conta_tiny" in url
    assert payload == {"conta_tiny": "conta_x", "ultima_busca_iso": "2026-09-18T12:00:00+00:00"}
    assert headers["Prefer"] == "resolution=merge-duplicates"
