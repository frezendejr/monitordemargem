import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from recalcular_margens import recalcular

# Lixeira no ML (pedido real 2000019675997270, 20/11/2026), como estava gravada:
# imposto 4% sobre o repasse (0,49), CMV 10,50, Ads 8% da venda so no pedido.
PEDIDO_LIXEIRA_ML = {
    "conta_tiny": "conta_amoshoesgyn",
    "numero_pedido": "2000019675997270",
    "canal": "meli_conta_1",
    "receita": 0.49,
    "valor_venda": 22.23,
    "cmv": 10.5,
    "imposto": 0.02,
    "margem_contribuicao": -11.81,
}
ITEM_LIXEIRA_ML = {
    "conta_tiny": "conta_amoshoesgyn",
    "numero_pedido": "2000019675997270",
    "sku": "lixeirapreto-1076",
    "canal": "meli_conta_1",
    "quantidade": 1,
    "receita": 0.49,
    "cmv": 10.5,
    "margem_contribuicao": -10.07,  # item tinha Ads sobre o repasse - nao batia com o pedido
    "custo_ausente": False,
}
# Minibike na Amazon (Tiny): receita = valor da venda.
PEDIDO_MINIBIKE = {
    "conta_tiny": "conta_inamorato",
    "numero_pedido": "926773159",
    "canal": "amazon",
    "receita": 123.99,
    "valor_venda": 123.99,
    "cmv": 53.77,
    "imposto": 4.96,
    "margem_contribuicao": 26.71,
}
ITEM_MINIBIKE = {
    "conta_tiny": "conta_inamorato",
    "numero_pedido": "926773159",
    "sku": "minibike-c1-dba",
    "canal": "amazon",
    "quantidade": 1,
    "receita": 123.99,
    "cmv": 53.77,
    "margem_contribuicao": 26.71,
    "custo_ausente": False,
}


def test_lixeira_ml_imposto_sobre_a_venda_e_custo_novo():
    pedidos, itens, resumo = recalcular(
        [PEDIDO_LIXEIRA_ML], [ITEM_LIXEIRA_ML], 7.5, {"lixeirapreto-1076": 11.19}
    )

    assert len(pedidos) == 1
    p = pedidos[0]
    assert p["imposto"] == 1.67  # 7,5% de 22,23
    assert p["cmv"] == 11.19
    # mesma conta que o monitor faz hoje: 0,49 - 11,19 - 1,67 - Ads 1,78
    assert p["margem_contribuicao"] == pytest.approx(0.49 - 11.19 - 1.67 - 1.78, abs=0.01)
    assert set(p) == {"conta_tiny", "numero_pedido", "canal", "imposto", "cmv", "margem_contribuicao", "margem_pct"}

    (item,) = itens
    assert item["cmv"] == 11.19
    assert item["margem_contribuicao"] == p["margem_contribuicao"]  # item volta a bater com o pedido
    assert resumo["pedidos_alterados"] == 1


def test_amazon_so_muda_o_imposto():
    pedidos, itens, _ = recalcular([PEDIDO_MINIBIKE], [ITEM_MINIBIKE], 7.5, {"lixeirapreto-1076": 11.19})

    p = pedidos[0]
    assert p["imposto"] == 9.30  # 7,5% de 123,99
    assert p["cmv"] == 53.77
    assert p["margem_contribuicao"] == pytest.approx(26.71 - (9.30 - 4.96), abs=0.01)
    assert itens[0]["margem_contribuicao"] == p["margem_contribuicao"]


def test_rodar_de_novo_nao_muda_nada():
    pedidos, itens, _ = recalcular([PEDIDO_LIXEIRA_ML], [ITEM_LIXEIRA_ML], 7.5, {"lixeirapreto-1076": 11.19})
    pedido_gravado = {**PEDIDO_LIXEIRA_ML, **pedidos[0]}
    item_gravado = {**ITEM_LIXEIRA_ML, **itens[0]}

    pedidos2, itens2, resumo2 = recalcular([pedido_gravado], [item_gravado], 7.5, {"lixeirapreto-1076": 11.19})

    assert pedidos2 == [] and itens2 == []
    assert resumo2["pedidos_alterados"] == 0


def test_pedido_sem_detalhamento_fica_como_esta():
    antigo = {**PEDIDO_MINIBIKE, "imposto": None, "valor_venda": None, "cmv": None}
    pedidos, itens, resumo = recalcular([antigo], [ITEM_MINIBIKE], 7.5, {})

    assert pedidos == [] and itens == []
    assert resumo["sem_detalhe_ignorados"] == 1


def test_item_com_custo_ausente_nao_ganha_cmv_aqui():
    # quem preenche esse e o recalcular_custo_pendente do monitor - se os dois
    # mexessem, o CMV seria descontado duas vezes
    item_sem_custo = {**ITEM_LIXEIRA_ML, "cmv": 0.0, "custo_ausente": True}
    pedido = {**PEDIDO_LIXEIRA_ML, "cmv": 0.0}
    pedidos, itens, _ = recalcular([pedido], [item_sem_custo], 7.5, {"lixeirapreto-1076": 11.19})

    assert pedidos[0]["cmv"] == 0.0  # so o imposto mudou
    assert pedidos[0]["margem_contribuicao"] == pytest.approx(-11.81 - (1.67 - 0.02), abs=0.01)
    assert [i["cmv"] for i in itens] == [0.0]


def test_rateio_do_pedido_com_dois_itens_soma_a_margem_do_pedido():
    pedido = {
        "conta_tiny": "c",
        "numero_pedido": "1",
        "canal": "shopee_1",
        "receita": 60.0,
        "valor_venda": 80.0,
        "cmv": 30.0,
        "imposto": 2.4,
        "margem_contribuicao": 10.0,
    }
    itens = [
        {"conta_tiny": "c", "numero_pedido": "1", "sku": "kit2lixeirapreto-1076", "canal": "shopee_1",
         "quantidade": 1, "receita": 40.0, "cmv": 20.0, "margem_contribuicao": 6.0, "custo_ausente": False},
        {"conta_tiny": "c", "numero_pedido": "1", "sku": "outro", "canal": "shopee_1",
         "quantidade": 1, "receita": 20.0, "cmv": 10.0, "margem_contribuicao": 4.0, "custo_ausente": False},
    ]
    pedidos_novos, itens_novos, _ = recalcular([pedido], itens, 7.5, {"kit2lixeirapreto-1076": 22.38})

    margem_pedido = pedidos_novos[0]["margem_contribuicao"]
    assert margem_pedido == pytest.approx(10.0 - (6.0 - 2.4) - (22.38 - 20.0), abs=0.01)
    assert sum(i["margem_contribuicao"] for i in itens_novos) == pytest.approx(margem_pedido, abs=0.02)
    assert {i["sku"]: i["cmv"] for i in itens_novos} == {"kit2lixeirapreto-1076": 22.38, "outro": 10.0}


class _Resp:
    def __init__(self, dados=None):
        self._dados = dados or []

    def raise_for_status(self):
        pass

    def json(self):
        return self._dados


def _supabase_fake(monkeypatch, gravacoes):
    import recalcular_margens

    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr(recalcular_margens, "load_dotenv", lambda: None)

    def _get(url, params, headers, timeout):
        gravacoes["gets"].append((url.rsplit("/", 1)[-1], params))
        if url.endswith("margin_monitor_custos"):
            return _Resp([{"sku": "lixeirapreto-1076", "custo": 11.19}])
        if url.endswith("margin_monitor_pedidos"):
            return _Resp([PEDIDO_LIXEIRA_ML, PEDIDO_MINIBIKE])
        return _Resp([ITEM_LIXEIRA_ML, ITEM_MINIBIKE])

    def _post(url, json, headers, timeout):
        gravacoes["posts"].append((url.rsplit("/", 1)[-1], json))
        return _Resp()

    monkeypatch.setattr(recalcular_margens.requests, "get", _get)
    monkeypatch.setattr(recalcular_margens.requests, "post", _post)
    return recalcular_margens


def test_cli_sem_aplicar_so_simula(monkeypatch, capsys):
    gravacoes = {"gets": [], "posts": []}
    modulo = _supabase_fake(monkeypatch, gravacoes)
    monkeypatch.setattr(sys, "argv", ["x", "--mes", "11/2026", "--imposto", "7.5", "--skus", "lixeirapreto-1076"])

    assert modulo.main() == 0
    assert gravacoes["posts"] == []
    tabelas = {tabela: params for tabela, params in gravacoes["gets"]}
    assert tabelas["margin_monitor_pedidos"]["data_pedido"] == "like.*/11/2026"
    assert tabelas["margin_monitor_custos"]["sku"] == 'in.("lixeirapreto-1076")'
    assert "SIMULACAO" in capsys.readouterr().out


def test_cli_aplicar_grava_pedidos_e_itens(monkeypatch):
    gravacoes = {"gets": [], "posts": []}
    modulo = _supabase_fake(monkeypatch, gravacoes)
    monkeypatch.setattr(
        sys, "argv", ["x", "--mes", "11/2026", "--imposto", "7.5", "--skus", "lixeirapreto-1076", "--aplicar"]
    )

    assert modulo.main() == 0
    destinos = [destino for destino, _ in gravacoes["posts"]]
    assert destinos == [
        "margin_monitor_pedidos?on_conflict=conta_tiny,numero_pedido",
        "margin_monitor_itens?on_conflict=conta_tiny,numero_pedido,sku",
    ]
    assert len(gravacoes["posts"][0][1]) == 2  # lixeira e minibike


def test_cli_recusa_sku_sem_custo_cadastrado(monkeypatch):
    gravacoes = {"gets": [], "posts": []}
    modulo = _supabase_fake(monkeypatch, gravacoes)
    monkeypatch.setattr(
        sys, "argv", ["x", "--mes", "11/2026", "--imposto", "7.5", "--skus", "lixeirapreto-1076,nao-existe", "--aplicar"]
    )

    assert modulo.main() == 1
    assert gravacoes["posts"] == []
