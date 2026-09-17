import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage
import monitor
from margin_engine import ItemPedido
from ml_client import MLApiError
from tiny_client import TinyApiError


CANAL_MELI_2_COM_ML = {
    "imposto_pct": 4.0,
    "ads_pct": 0.0,
    "comissao_pct": 14.0,
    "frete_pct": 15.0,
    "receita_exata_via_ml": True,
}

CANAL_SEM_ML = {
    "imposto_pct": 4.0,
    "ads_pct": 15.0,
    "comissao_pct": 14.0,
    "frete_pct": 0.0,
}


class _MLClientFake:
    def __init__(self, receita=None, erro=False):
        self._receita = receita
        self._erro = erro

    def obter_receita_liquida_pedido(self, numero_ecommerce):
        if self._erro:
            raise MLApiError("falha simulada")
        return self._receita


def test_canal_sem_flag_usa_total_pedido_do_tiny():
    pedido = {"total_pedido": "100.00", "numero_ecommerce": "X"}
    receita, canal_cfg = monitor._resolver_receita_e_config(pedido, "shopee_1", CANAL_SEM_ML, {})

    assert receita == 100.0
    assert canal_cfg is CANAL_SEM_ML  # nao mexeu no config


def test_canal_com_flag_usa_receita_exata_e_zera_comissao_frete(monkeypatch):
    pedido = {"total_pedido": "145.49", "numero_ecommerce": "2000018504802760"}
    ml_clientes = {"meli_conta_2": _MLClientFake(receita=115.45)}

    receita, canal_cfg = monitor._resolver_receita_e_config(
        pedido, "meli_conta_2", CANAL_MELI_2_COM_ML, ml_clientes
    )

    assert receita == 115.45
    assert canal_cfg["comissao_pct"] == 0.0
    assert canal_cfg["frete_pct"] == 0.0
    assert canal_cfg["imposto_pct"] == 4.0  # imposto continua estimado


def test_falha_na_api_do_ml_cai_para_estimativa_do_tiny():
    pedido = {"total_pedido": "145.49", "numero_ecommerce": "2000018504802760"}
    ml_clientes = {"meli_conta_2": _MLClientFake(erro=True)}

    receita, canal_cfg = monitor._resolver_receita_e_config(
        pedido, "meli_conta_2", CANAL_MELI_2_COM_ML, ml_clientes
    )

    assert receita == 145.49
    assert canal_cfg["comissao_pct"] == 14.0  # nao zerou, usou o config original


CONFIG_SEM_ALERTA = {
    "alertas": {"margem_negativa_threshold": 0, "whatsapp": {"ativo": False}, "email": {"ativo": False}}
}


class _TinyClientFake:
    """Simula 2 pedidos atualizados; o segundo falha com TinyApiError
    (ex.: rate limit) se `falha_no_segundo=True`."""

    def __init__(self, falha_no_segundo: bool):
        self._falha_no_segundo = falha_no_segundo

    def buscar_pedidos_atualizados_desde(self, desde):
        return [{"id": "1001"}, {"id": "1002"}]

    def obter_pedido_completo(self, id_tiny):
        if id_tiny == "1002" and self._falha_no_segundo:
            raise TinyApiError("API Bloqueada - rate limit simulado")
        return {"total_pedido": "100.00", "numero_ecommerce": "X", "itens": []}

    def identificar_canal(self, pedido, canais_config):
        return "shopee_1"

    def montar_itens(self, pedido, custo_cache):
        return [ItemPedido(sku="ABC", quantidade=1, valor_unitario=100.0, custo_unitario=40.0)]

    def obter_custo_produto(self, sku):
        return 40.0


class _TinyClientCustoFake:
    """2 pedidos que usam o MESMO id_produto. A primeira busca de custo
    desse produto falha (rate limit simulado); a segunda tem sucesso."""

    def __init__(self):
        self._chamadas_custo = 0

    def buscar_pedidos_atualizados_desde(self, desde):
        return [{"id": "2001"}, {"id": "2002"}]

    def obter_pedido_completo(self, id_tiny):
        return {
            "total_pedido": "100.00",
            "numero_ecommerce": "X",
            "itens": [{"item": {"codigo": "SKU1", "id_produto": "PROD1", "quantidade": "1.00", "valor_unitario": "100.00"}}],
        }

    def identificar_canal(self, pedido, canais_config):
        return "shopee_1"

    def obter_custo_produto(self, id_produto):
        self._chamadas_custo += 1
        if self._chamadas_custo == 1:
            raise TinyApiError("API Bloqueada - rate limit simulado")
        return 40.0

    def montar_itens(self, pedido, custo_cache):
        item = pedido["itens"][0]["item"]
        return [
            ItemPedido(
                sku=item["codigo"],
                quantidade=1,
                valor_unitario=100.0,
                custo_unitario=custo_cache.get(item["id_produto"]),
            )
        ]


def test_falha_de_api_no_custo_nao_envenena_o_cache_pro_resto_da_rodada(tmp_path):
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientCustoFake()
        monitor.processar_ciclo_conta("conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn)

        db_path = tmp_path / "teste.db"
        import sqlite3

        c = sqlite3.connect(str(db_path))
        pedido_2001 = c.execute(
            "SELECT custo_ausente FROM pedidos_processados WHERE numero_pedido='2001'"
        ).fetchone()
        pedido_2002 = c.execute(
            "SELECT custo_ausente FROM pedidos_processados WHERE numero_pedido='2002'"
        ).fetchone()

        assert pedido_2001[0] == 1  # primeira tentativa falhou (rate limit simulado)
        assert pedido_2002[0] == 0  # segunda tentativa do MESMO produto teve sucesso


def test_checkpoint_avanca_quando_ciclo_completa_sem_falha(tmp_path):
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=False)
        monitor.processar_ciclo_conta("conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn)

        assert storage.obter_checkpoint(conn, "conta_x") is not None


def test_checkpoint_nao_avanca_quando_algum_pedido_falha(tmp_path):
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=True)
        monitor.processar_ciclo_conta("conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn)

        # pedido 1001 foi processado (nao se perde o que deu certo)...
        assert storage.ja_processado(conn, "conta_x", "1001") is True
        # ...mas o checkpoint fica intacto, pra 1002 ser tentado de novo depois
        assert storage.obter_checkpoint(conn, "conta_x") is None
