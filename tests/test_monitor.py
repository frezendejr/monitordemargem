import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage
import monitor
from margin_engine import ItemPedido
from ml_client import DetalheFinanceiroPedido, MLApiError
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
    def __init__(self, receita=None, erro=False, valor_venda=None, comissao_real=0.0, frete_real=0.0):
        self._receita = receita
        self._erro = erro
        self._valor_venda = valor_venda if valor_venda is not None else receita
        self._comissao_real = comissao_real
        self._frete_real = frete_real

    def obter_detalhe_financeiro_pedido(self, numero_ecommerce):
        if self._erro:
            raise MLApiError("falha simulada")
        return DetalheFinanceiroPedido(
            receita_liquida=self._receita,
            valor_venda=self._valor_venda,
            comissao_real=self._comissao_real,
            frete_real=self._frete_real,
        )


def test_canal_sem_flag_usa_total_pedido_do_tiny():
    pedido = {"total_pedido": "100.00", "numero_ecommerce": "X"}
    receita, canal_cfg, detalhe = monitor._resolver_receita_e_config(pedido, "shopee_1", CANAL_SEM_ML, {})

    assert receita == 100.0
    assert canal_cfg is CANAL_SEM_ML  # nao mexeu no config
    assert detalhe is None


def test_canal_com_flag_usa_receita_exata_e_zera_comissao_frete(monkeypatch):
    pedido = {"total_pedido": "145.49", "numero_ecommerce": "2000018504802760"}
    ml_clientes = {"meli_conta_2": _MLClientFake(receita=115.45, valor_venda=180.0, comissao_real=47.5, frete_real=17.05)}

    receita, canal_cfg, detalhe = monitor._resolver_receita_e_config(
        pedido, "meli_conta_2", CANAL_MELI_2_COM_ML, ml_clientes
    )

    assert receita == 115.45
    assert canal_cfg["comissao_pct"] == 0.0
    assert canal_cfg["frete_pct"] == 0.0
    assert canal_cfg["imposto_pct"] == 4.0  # imposto continua estimado
    assert detalhe.valor_venda == 180.0
    assert detalhe.comissao_real == 47.5
    assert detalhe.frete_real == 17.05


def test_falha_na_api_do_ml_cai_para_estimativa_do_tiny():
    pedido = {"total_pedido": "145.49", "numero_ecommerce": "2000018504802760"}
    ml_clientes = {"meli_conta_2": _MLClientFake(erro=True)}

    receita, canal_cfg, detalhe = monitor._resolver_receita_e_config(
        pedido, "meli_conta_2", CANAL_MELI_2_COM_ML, ml_clientes
    )

    assert receita == 145.49
    assert canal_cfg["comissao_pct"] == 14.0  # nao zerou, usou o config original
    assert detalhe is None


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
        return {
            "total_pedido": "100.00",
            "numero_ecommerce": "X",
            "itens": [{"item": {"codigo": "ABC", "id_produto": "1", "quantidade": "1.00", "valor_unitario": "100.00"}}],
        }

    def identificar_canal(self, pedido, canais_config):
        return "shopee_1"

    def montar_itens(self, pedido, custos):
        item = pedido["itens"][0]["item"]
        return [
            ItemPedido(
                sku=item["codigo"],
                quantidade=1,
                valor_unitario=100.0,
                custo_unitario=custos.get(item["codigo"]),
            )
        ]


def _sem_supabase(monkeypatch):
    """processar_ciclo_conta chama supabase_writer.enviar_pedido/enviar_itens
    DE VERDADE (so alimenta o dashboard, nunca falha o teste) - se por
    acaso SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY reais estiverem no
    ambiente (ex.: outro modulo chamou load_dotenv() antes, no mesmo
    processo do pytest), esses testes gravavam pedido de teste de verdade
    no Supabase de producao (bug real, achado em 2026-09-19: "conta_x",
    pedido "1001"/"1002", sku "ABC" apareceram no dashboard como se fosse
    venda real). Nunca deixa esse side-effect escapar do teste."""
    monkeypatch.setattr(monitor.supabase_writer, "enviar_pedido", lambda *a, **k: None)
    monkeypatch.setattr(monitor.supabase_writer, "enviar_itens", lambda *a, **k: None)


def test_custo_vem_da_planilha_nao_do_tiny(tmp_path, monkeypatch):
    _sem_supabase(monkeypatch)
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=False)
        custos = {"ABC": 40.0}
        monitor.processar_ciclo_conta(
            "conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn, custos
        )

        resultado = conn.execute(
            "SELECT custo_ausente FROM pedidos_processados WHERE numero_pedido='1001'"
        ).fetchone()
        assert resultado[0] == 0  # achou o custo na planilha


def test_sku_fora_da_planilha_vira_custo_ausente(tmp_path, monkeypatch):
    _sem_supabase(monkeypatch)
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=False)
        monitor.processar_ciclo_conta(
            "conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn, custos={}
        )

        resultado = conn.execute(
            "SELECT custo_ausente FROM pedidos_processados WHERE numero_pedido='1001'"
        ).fetchone()
        assert resultado[0] == 1  # SKU "ABC" nao esta na planilha de custo


def test_checkpoint_avanca_quando_ciclo_completa_sem_falha(tmp_path, monkeypatch):
    _sem_supabase(monkeypatch)
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=False)
        monitor.processar_ciclo_conta(
            "conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn, custos={}
        )

        assert storage.obter_checkpoint(conn, "conta_x") is not None


def test_checkpoint_nao_avanca_quando_algum_pedido_falha(tmp_path, monkeypatch):
    _sem_supabase(monkeypatch)
    with storage.sessao(str(tmp_path / "teste.db")) as conn:
        cliente = _TinyClientFake(falha_no_segundo=True)
        monitor.processar_ciclo_conta(
            "conta_x", cliente, {"shopee_1": CANAL_SEM_ML}, CONFIG_SEM_ALERTA, conn, custos={}
        )

        # pedido 1001 foi processado (nao se perde o que deu certo)...
        assert storage.ja_processado(conn, "conta_x", "1001") is True
        # ...mas o checkpoint fica intacto, pra 1002 ser tentado de novo depois
        assert storage.obter_checkpoint(conn, "conta_x") is None
