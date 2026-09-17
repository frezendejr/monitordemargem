import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from margin_engine import ItemPedido, calcular_margem


CANAL_MELI_1 = {
    "imposto_pct": 4.0,
    "ads_pct": 0.0,
    "comissao_pct": 14.0,
    "faixas_frete": [
        {"ate_valor": 79.00, "frete_pct": 40.0},
        {"ate_valor": None, "frete_pct": 15.0},
    ],
}

CANAL_SHOPEE_1 = {
    "imposto_pct": 4.0,
    "ads_pct": 15.0,
    "comissao_pct": 14.0,
    "frete_pct": 0.0,
}


def test_margem_positiva_pedido_alto_valor():
    itens = [ItemPedido(sku="ABC123", quantidade=1, valor_unitario=200.0, custo_unitario=80.0)]
    resultado = calcular_margem("1001", "meli_conta_1", itens, 200.0, CANAL_MELI_1)

    assert resultado.receita == 200.0
    assert resultado.cmv == 80.0
    assert resultado.custo_ausente is False
    # faixa acima de 79 -> frete_pct 15%
    assert resultado.frete == 30.0
    assert resultado.margem_contribuicao > 0


def test_regra_dos_79_aplica_frete_maior():
    itens = [ItemPedido(sku="ABC123", quantidade=1, valor_unitario=50.0, custo_unitario=30.0)]
    resultado = calcular_margem("1002", "meli_conta_1", itens, 50.0, CANAL_MELI_1)

    # receita 50 <= 79 -> frete_pct 40%
    assert resultado.frete == 20.0


def test_margem_negativa_custo_alto():
    itens = [ItemPedido(sku="ABC123", quantidade=1, valor_unitario=50.0, custo_unitario=45.0)]
    resultado = calcular_margem("1003", "meli_conta_1", itens, 50.0, CANAL_MELI_1)

    assert resultado.margem_contribuicao < 0


def test_custo_ausente_nao_e_escondido():
    itens = [ItemPedido(sku="SEMCUSTO", quantidade=2, valor_unitario=100.0, custo_unitario=None)]
    resultado = calcular_margem("1004", "shopee_1", itens, 200.0, CANAL_SHOPEE_1)

    assert resultado.custo_ausente is True
    assert resultado.skus_sem_custo == ["SEMCUSTO"]
    # cmv fica 0 (nao inventa custo), margem calculada e otimista mas o flag denuncia isso
    assert resultado.cmv == 0.0


def test_ads_rateado_sobre_receita_shopee():
    itens = [ItemPedido(sku="XYZ", quantidade=1, valor_unitario=100.0, custo_unitario=40.0)]
    resultado = calcular_margem("1005", "shopee_1", itens, 100.0, CANAL_SHOPEE_1)

    assert resultado.ads == 15.0  # 15% de 100


def test_receita_vem_pronta_do_total_pedido_ja_liquido_de_desconto():
    # Ex.: TikTok Shop com cupom - total_produtos=159.99, desconto=100,
    # frete=1.99, total_pedido (ja liquido) = 61.98. calcular_margem nao
    # recalcula nada, so usa o total_pedido que o Tiny ja devolveu.
    itens = [ItemPedido(sku="XYZ", quantidade=1, valor_unitario=159.99, custo_unitario=40.0)]
    resultado = calcular_margem("1006", "shopee_1", itens, receita=61.98, canal_config=CANAL_SHOPEE_1)

    assert resultado.receita == 61.98
