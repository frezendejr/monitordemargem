"""Pedido que o Tiny importa antes de o marketplace liberar o preco (ex.: Amazon
"Pending"): total_pedido = 0 e itens com valor_unitario = 0. Caso real que motivou
estes testes: Amazon 926773159 (minibike vendida a R$ 123,99) gravado em 20/11/2026
com receita 0 e margem -R$ 58,27."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests

import monitor
import monitor_cloud
import storage
from margin_engine import ItemPedido
from tiny_client import TinyClient, pedido_sem_valor

CANAL_AMAZON = {
    "tiny_identificador": "Amazon",
    "imposto_pct": 4.0,
    "ads_pct": 0.0,
    "comissao_pct": 15.0,
    "frete_pct": 0.0,
    "dba": {
        "origem": "interior_sul_sudeste",
        "faixa_padrao": "1-2kg",
        "faixas_por_sku": [{"padrao": "minibike|pj198", "faixa": "2-3kg"}],
    },
}

CONFIG = {
    "alertas": {"margem_negativa_threshold": 0, "whatsapp": {"ativo": False}, "email": {"ativo": False}},
    "tiny_contas": {
        "conta_inamorato": {"token_env": "TINY_API_TOKEN_INAMORATO", "canais": {"amazon": CANAL_AMAZON}},
        "conta_amoshoesgyn": {
            "token_env": "TINY_API_TOKEN_AMOSHOESGYN",
            "canais": {"meli_conta_1": {"fonte_pedido": "ml", "imposto_pct": 4.0}},
        },
    },
}

CUSTOS = {"minibike-c1-dba": 53.77}


def _pedido_tiny(valor: str, situacao: str = "Aprovado") -> dict:
    return {
        "total_pedido": valor,
        "numero_ecommerce": "701-8259409-2544257",
        "data_pedido": "20/11/2026",
        "situacao": situacao,
        "ecommerce": {"nomeEcommerce": "Amazon"},
        "itens": [
            {"item": {"codigo": "minibike-c1-dba", "id_produto": "1", "quantidade": "1.00", "valor_unitario": valor}}
        ],
    }


class _TinyFake:
    """Usa o montar_itens/identificar_canal de verdade; so o acesso a API e fake."""

    def __init__(self, pedidos: dict[str, dict], resumos: list[dict] | None = None):
        self._pedidos = pedidos
        self._resumos = resumos or [{"id": k} for k in pedidos]
        self.lidos: list[str] = []

    def buscar_pedidos_atualizados_desde(self, desde):
        return self._resumos

    def obter_pedido_completo(self, id_tiny):
        self.lidos.append(id_tiny)
        return self._pedidos[id_tiny]

    identificar_canal = TinyClient.identificar_canal
    montar_itens = staticmethod(TinyClient.montar_itens)


@pytest.fixture
def gravacoes(monkeypatch):
    """Nunca deixa teste tocar Supabase/WhatsApp/e-mail de verdade."""
    registro = {"pedidos": [], "itens": [], "alertas": [], "checkpoint": None}
    monkeypatch.setattr(
        monitor_cloud.supabase_writer,
        "enviar_pedido",
        lambda conta, resultado, alertado, valor_venda=None: registro["pedidos"].append((conta, resultado, alertado, valor_venda)),
    )
    monkeypatch.setattr(
        monitor_cloud.supabase_writer,
        "enviar_itens",
        lambda conta, resultado, anuncios=None, numero_ecommerce=None: registro["itens"].append((conta, resultado, numero_ecommerce)),
    )
    monkeypatch.setattr(monitor_cloud, "enviar_whatsapp", lambda msg, cfg: registro["alertas"].append(msg))
    monkeypatch.setattr(monitor_cloud, "enviar_email", lambda assunto, msg, cfg: registro["alertas"].append(msg))
    monkeypatch.setattr(monitor_cloud.storage, "obter_checkpoint", lambda conta: None)
    monkeypatch.setattr(monitor_cloud.storage, "ja_processado", lambda conta, numero: False)
    monkeypatch.setattr(
        monitor_cloud.storage, "salvar_checkpoint", lambda conta, quando: registro.__setitem__("checkpoint", quando)
    )
    return registro


# --------------------------------------------------------------------------
# tiny_client.pedido_sem_valor
# --------------------------------------------------------------------------


def test_total_e_itens_zerados_e_pedido_sem_valor():
    itens = [ItemPedido(sku="minibike-c1-dba", quantidade=1, valor_unitario=0.0, custo_unitario=53.77)]
    assert pedido_sem_valor({"total_pedido": "0.00"}, itens) is True
    assert pedido_sem_valor({"total_pedido": None}, itens) is True
    assert pedido_sem_valor({}, itens) is True


def test_pedido_com_valor_nao_e_barrado():
    itens = [ItemPedido(sku="minibike-c1-dba", quantidade=1, valor_unitario=123.99, custo_unitario=53.77)]
    assert pedido_sem_valor({"total_pedido": "123.99"}, itens) is False


def test_total_zerado_mas_item_com_valor_nao_e_barrado():
    # ex.: cupom de 100% - os itens tem preco, so o total zerou
    itens = [ItemPedido(sku="x", quantidade=1, valor_unitario=50.0, custo_unitario=10.0)]
    assert pedido_sem_valor({"total_pedido": "0.00"}, itens) is False


# --------------------------------------------------------------------------
# monitor_cloud.processar_conta (ciclo normal)
# --------------------------------------------------------------------------


def test_ciclo_nao_grava_nem_alerta_pedido_sem_valor(gravacoes):
    cliente = _TinyFake({"926773159": _pedido_tiny("0.00"), "926773296": _pedido_tiny("123.99")})

    processados = monitor_cloud.processar_conta(
        "conta_inamorato", cliente, {"amazon": CANAL_AMAZON}, CONFIG, CUSTOS
    )

    assert processados == 1
    gravados = [resultado.numero_pedido for _, resultado, _, _ in gravacoes["pedidos"]]
    assert gravados == ["926773296"]  # o sem valor ficou de fora
    assert gravacoes["alertas"] == []  # e nao disparou o falso alerta de prejuizo
    assert gravacoes["checkpoint"] is not None  # pedido sem valor nao trava o checkpoint


def test_ciclo_calcula_minibike_amazon_com_o_valor():
    resultado, valor_venda, detalhe = monitor_cloud._calcular_pedido_tiny(
        "926773159",
        _pedido_tiny("123.99"),
        "amazon",
        CANAL_AMAZON,
        TinyClient.montar_itens(_pedido_tiny("123.99"), CUSTOS),
        {},
    )
    # 123,99 - CMV 53,77 - imposto 4% (4,96) - comissao 15% (18,60) - DBA 2-3kg interior (19,95)
    assert resultado.receita == 123.99
    assert resultado.imposto == 4.96
    assert resultado.comissao == 18.60
    assert resultado.frete == 19.95
    assert resultado.margem_contribuicao == pytest.approx(26.71, abs=0.01)
    assert valor_venda == 123.99
    assert detalhe is None


def test_receita_exata_do_ml_paga_imposto_e_ads_sobre_o_valor_de_venda():
    from ml_client import DetalheFinanceiroPedido

    class _MLFake:
        def obter_detalhe_financeiro_pedido(self, numero_ecommerce):
            return DetalheFinanceiroPedido(
                receita_liquida=0.49, valor_venda=22.23, comissao_real=4.00, frete_real=17.74, anuncios={}
            )

    canal_ml = {"imposto_pct": 7.5, "ads_pct": 8.0, "comissao_pct": 14.0, "frete_pct": 15.0, "receita_exata_via_ml": True}
    pedido = {"total_pedido": "22.23", "numero_ecommerce": "2000019675997270", "data_pedido": "20/11/2026"}
    itens = [ItemPedido(sku="lixeirapreto-1076", quantidade=1, valor_unitario=22.23, custo_unitario=11.19)]

    resultado, valor_venda, detalhe = monitor_cloud._calcular_pedido_tiny(
        "900001", pedido, "meli_conta_1", canal_ml, itens, {"meli_conta_1": _MLFake()}
    )

    assert valor_venda == 22.23
    assert resultado.receita == 0.49  # repasse real do Mercado Pago
    assert resultado.imposto == 1.67  # 7,5% da venda, nao do repasse
    assert resultado.ads == 1.78  # 8% da venda
    assert (resultado.comissao, resultado.frete) == (4.00, 17.74)  # so informativos
    assert resultado.margem_contribuicao == pytest.approx(0.49 - 11.19 - 1.67 - 1.78, abs=0.01)
    assert sum(i.margem_contribuicao for i in resultado.itens) == pytest.approx(resultado.margem_contribuicao, abs=0.01)


# --------------------------------------------------------------------------
# monitor_cloud.recalcular_pedidos_sem_valor (pedidos ja gravados com receita 0)
# --------------------------------------------------------------------------


def test_recalcula_pedido_gravado_sem_valor_quando_o_valor_chega(gravacoes, monkeypatch):
    monkeypatch.setattr(
        monitor_cloud,
        "_pedidos_gravados_sem_valor",
        lambda desde: [
            {"conta_tiny": "conta_inamorato", "numero_pedido": "926773159", "canal": "amazon"},
            {"conta_tiny": "conta_inamorato", "numero_pedido": "926000001", "canal": "amazon"},
            # ML vem da API do ML (numero do ML, nao do Tiny) - nunca rele no Tiny
            {"conta_tiny": "conta_amoshoesgyn", "numero_pedido": "2000019675997270", "canal": "meli_conta_1"},
        ],
    )
    tiny = _TinyFake({"926773159": _pedido_tiny("123.99"), "926000001": _pedido_tiny("0.00")})

    corrigidos = monitor_cloud.recalcular_pedidos_sem_valor(CONFIG, CUSTOS, clientes={"conta_inamorato": tiny})

    assert corrigidos == 1
    assert tiny.lidos == ["926773159", "926000001"]  # o do ML nem foi consultado no Tiny
    assert len(gravacoes["pedidos"]) == 1
    conta, resultado, alertado, valor_venda = gravacoes["pedidos"][0]
    assert (conta, resultado.numero_pedido) == ("conta_inamorato", "926773159")
    assert resultado.receita == 123.99
    assert valor_venda == 123.99
    assert resultado.margem_contribuicao == pytest.approx(26.71, abs=0.01)
    assert alertado is False
    # itens regravados (upsert) com o numero da venda na Amazon
    assert [(c, r.numero_pedido, n) for c, r, n in gravacoes["itens"]] == [
        ("conta_inamorato", "926773159", "701-8259409-2544257")
    ]
    assert gravacoes["alertas"] == []


def test_pedido_cancelado_nao_e_recalculado(gravacoes, monkeypatch):
    monkeypatch.setattr(
        monitor_cloud,
        "_pedidos_gravados_sem_valor",
        lambda desde: [{"conta_tiny": "conta_inamorato", "numero_pedido": "926773159", "canal": "amazon"}],
    )
    tiny = _TinyFake({"926773159": _pedido_tiny("123.99", situacao="Cancelado")})

    assert monitor_cloud.recalcular_pedidos_sem_valor(CONFIG, CUSTOS, clientes={"conta_inamorato": tiny}) == 0
    assert gravacoes["pedidos"] == []


def test_falha_no_supabase_nao_derruba_o_ciclo(gravacoes, monkeypatch):
    def _falha(desde):
        raise requests.ConnectionError("sem rede")

    monkeypatch.setattr(monitor_cloud, "_pedidos_gravados_sem_valor", _falha)

    assert monitor_cloud.recalcular_pedidos_sem_valor(CONFIG, CUSTOS, clientes={}) == 0


def test_busca_no_supabase_pede_so_receita_zero_na_janela(monkeypatch):
    chamadas = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr(
        monitor_cloud.requests, "get", lambda url, params, headers, timeout: chamadas.append((url, params)) or _Resp()
    )
    from datetime import datetime, timezone

    monitor_cloud._pedidos_gravados_sem_valor(datetime(2026, 11, 13, tzinfo=timezone.utc))

    url, params = chamadas[0]
    assert url == "https://exemplo.supabase.co/rest/v1/margin_monitor_pedidos"
    assert params["receita"] == "eq.0"
    assert params["processado_em"] == "gte.2026-11-13T00:00:00+00:00"


# --------------------------------------------------------------------------
# monitor.py (versao local, SQLite) - mesma trava
# --------------------------------------------------------------------------


def test_monitor_local_tambem_nao_grava_pedido_sem_valor(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor.supabase_writer, "enviar_pedido", lambda *a, **k: None)
    monkeypatch.setattr(monitor.supabase_writer, "enviar_itens", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "atualizar_planilha", lambda *a, **k: None)
    cliente = _TinyFake({"926773159": _pedido_tiny("0.00"), "926773296": _pedido_tiny("123.99")})

    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        monitor.processar_ciclo_conta("conta_inamorato", cliente, {"amazon": CANAL_AMAZON}, CONFIG, conn, CUSTOS)

        assert storage.ja_processado(conn, "conta_inamorato", "926773159") is False
        assert storage.ja_processado(conn, "conta_inamorato", "926773296") is True
