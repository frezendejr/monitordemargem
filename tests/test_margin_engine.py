import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

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


def test_rateio_por_item_soma_bate_com_o_pedido_inteiro():
    itens = [
        ItemPedido(sku="A", quantidade=1, valor_unitario=150.0, custo_unitario=60.0),
        ItemPedido(sku="B", quantidade=2, valor_unitario=25.0, custo_unitario=10.0),
    ]
    resultado = calcular_margem("2001", "shopee_1", itens, receita=200.0, canal_config=CANAL_SHOPEE_1)

    assert len(resultado.itens) == 2
    soma_receita = sum(i.receita for i in resultado.itens)
    soma_margem = sum(i.margem_contribuicao for i in resultado.itens)
    assert soma_receita == pytest.approx(resultado.receita, abs=0.01)
    assert soma_margem == pytest.approx(resultado.margem_contribuicao, abs=0.01)


def test_rateio_por_item_pesa_pelo_valor_do_item():
    # Item A vale 3x mais que o item B (150 vs 50 de valor total) -> A deve
    # ficar com ~75% da receita rateada.
    itens = [
        ItemPedido(sku="A", quantidade=1, valor_unitario=150.0, custo_unitario=60.0),
        ItemPedido(sku="B", quantidade=1, valor_unitario=50.0, custo_unitario=20.0),
    ]
    resultado = calcular_margem("2002", "shopee_1", itens, receita=200.0, canal_config=CANAL_SHOPEE_1)

    item_a = next(i for i in resultado.itens if i.sku == "A")
    item_b = next(i for i in resultado.itens if i.sku == "B")
    assert item_a.receita == pytest.approx(150.0, abs=0.01)
    assert item_b.receita == pytest.approx(50.0, abs=0.01)


def test_rateio_por_item_marca_custo_ausente_so_no_item_sem_custo():
    itens = [
        ItemPedido(sku="COM_CUSTO", quantidade=1, valor_unitario=100.0, custo_unitario=40.0),
        ItemPedido(sku="SEM_CUSTO", quantidade=1, valor_unitario=100.0, custo_unitario=None),
    ]
    resultado = calcular_margem("2003", "shopee_1", itens, receita=200.0, canal_config=CANAL_SHOPEE_1)

    item_com = next(i for i in resultado.itens if i.sku == "COM_CUSTO")
    item_sem = next(i for i in resultado.itens if i.sku == "SEM_CUSTO")
    assert item_com.custo_ausente is False
    assert item_sem.custo_ausente is True
