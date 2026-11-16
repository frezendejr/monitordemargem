import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ml_client
from ml_client import BASE_URL, MLApiError, MLClient


class _Resp:
    def __init__(self, status_code: int, corpo=None):
        self.status_code = status_code
        self._corpo = corpo if corpo is not None else {}
        self.ok = 200 <= status_code < 300
        self.text = str(self._corpo)

    def json(self):
        return self._corpo


def _cliente(monkeypatch) -> MLClient:
    monkeypatch.setattr(MLClient, "_access_token_valido", lambda self: "token-fake")
    monkeypatch.setattr(ml_client.time, "sleep", lambda s: None)
    return MLClient("meli_conta_1", app_id="app", client_secret="segredo")


def _fake_get(respostas_item: dict, chamadas: list):
    """Simula /users/me, o scan de /users/{id}/items/search (2 paginas, com
    um id repetido) e /items/{id}. respostas_item: id -> lista de status
    devolvidos em sequencia (ultimo se repete)."""
    trava = threading.Lock()
    paginas = {None: (["MLB1", "MLB2", "MLB3"], "s1"), "s1": (["MLB3", "MLB4"], "s2"), "s2": ([], None)}

    def fake_get(url, headers=None, params=None, timeout=None):
        with trava:
            chamadas.append((url, dict(params or {})))
        if url == f"{BASE_URL}/users/me":
            return _Resp(200, {"id": 123})
        if url == f"{BASE_URL}/users/123/items/search":
            resultados, proximo = paginas[(params or {}).get("scroll_id")]
            return _Resp(200, {"results": resultados, "scroll_id": proximo})
        if url.startswith(f"{BASE_URL}/items/"):
            item_id = url.rsplit("/", 1)[1]
            with trava:
                fila = respostas_item[item_id]
                status = fila.pop(0) if len(fila) > 1 else fila[0]
            corpo = {"id": item_id, "status": "paused", "title": f"Anuncio {item_id}"} if status == 200 else {"error": status}
            return _Resp(status, corpo)
        raise AssertionError(f"URL inesperada: {url}")

    return fake_get


def test_busca_detalhe_um_a_um_ignora_404_e_refaz_429(monkeypatch):
    cliente = _cliente(monkeypatch)
    chamadas: list = []
    respostas = {"MLB1": [200], "MLB2": [404], "MLB3": [429, 200], "MLB4": [200]}
    monkeypatch.setattr(ml_client.requests, "get", _fake_get(respostas, chamadas))

    anuncios = cliente.obter_anuncios_por_status("paused")

    assert sorted(a["id"] for a in anuncios) == ["MLB1", "MLB3", "MLB4"]
    urls = [url for url, _ in chamadas]
    assert f"{BASE_URL}/items" not in urls  # multiget foi descontinuado pelo ML (403)
    assert urls.count(f"{BASE_URL}/items/MLB3") == 2  # 429 e depois 200
    assert urls.count(f"{BASE_URL}/items/MLB1") == 1  # id repetido no scan nao duplica


def test_erro_pontual_num_anuncio_so_tira_ele_da_lista(monkeypatch):
    cliente = _cliente(monkeypatch)
    respostas = {"MLB1": [200], "MLB2": [403], "MLB3": [200], "MLB4": [200]}
    monkeypatch.setattr(ml_client.requests, "get", _fake_get(respostas, []))

    anuncios = cliente.obter_anuncios_por_status("paused")

    assert sorted(a["id"] for a in anuncios) == ["MLB1", "MLB3", "MLB4"]


def test_429_persistente_desiste_depois_das_tentativas(monkeypatch):
    cliente = _cliente(monkeypatch)
    chamadas: list = []
    respostas = {"MLB1": [200], "MLB2": [200], "MLB3": [429], "MLB4": [200]}
    monkeypatch.setattr(ml_client.requests, "get", _fake_get(respostas, chamadas))

    anuncios = cliente.obter_anuncios_por_status("paused")

    assert sorted(a["id"] for a in anuncios) == ["MLB1", "MLB2", "MLB4"]
    assert [url for url, _ in chamadas].count(f"{BASE_URL}/items/MLB3") == 4


def test_metade_ou_mais_falhando_derruba_a_conta(monkeypatch):
    cliente = _cliente(monkeypatch)
    respostas = {"MLB1": [200], "MLB2": [403], "MLB3": [403], "MLB4": [200]}
    monkeypatch.setattr(ml_client.requests, "get", _fake_get(respostas, []))

    with pytest.raises(MLApiError, match="403"):
        cliente.obter_anuncios_por_status("paused")
