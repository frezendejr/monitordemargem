from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from minibike_controle import FUSO_BRASILIA, calcular_controle, filtrar_produto

LIBERACAO = datetime(2026, 10, 2, 20, 0, tzinfo=FUSO_BRASILIA)


def _item(canal, pedido, sku, quando_brt, qtd=1, receita=100.0, cmv=50.0, margem=30.0, sem_custo=False):
    utc = quando_brt.astimezone(timezone.utc).isoformat()
    return {"canal": canal, "numero_pedido": pedido, "sku": sku, "quantidade": qtd, "receita": receita,
            "cmv": cmv, "margem_contribuicao": margem, "custo_ausente": sem_custo, "processado_em": utc}


def test_filtra_so_variacoes_do_produto():
    df = pd.DataFrame([{"sku": "minibike-c1-dba"}, {"sku": "PJ198"}, {"sku": "minibikevickylab"}, {"sku": "tenis-123"}])
    assert sorted(filtrar_produto(df)["sku"]) == ["PJ198", "minibike-c1-dba", "minibikevickylab"]


def test_ritmo_usa_horas_expostas_e_nao_dia_calendario():
    # 4 vendas em 4h (20h-24h de 02/10) -> 24/dia, nao 4/dia
    agora = LIBERACAO + timedelta(hours=4)
    itens = pd.DataFrame([_item("amazon", str(i), "minibike-c1-dba", LIBERACAO + timedelta(minutes=30 + 50 * i)) for i in range(4)])
    r = calcular_controle(itens, 3180, LIBERACAO, agora)
    assert r.horas_expostas == pytest.approx(4)
    assert r.ritmo_dia == pytest.approx(24)
    assert r.estoque_atual == 3176
    assert r.dias_estoque == pytest.approx(3176 / 24)
    assert r.ritmo_24h is None  # ainda sem 24h completas


def test_total_por_canal_e_margem_por_unidade():
    agora = LIBERACAO + timedelta(hours=10)
    itens = pd.DataFrame([
        _item("amazon", "1", "minibike-c1-dba", LIBERACAO + timedelta(hours=1), qtd=1, receita=100, margem=40),
        _item("amazon", "2", "minibike-c1-dba", LIBERACAO + timedelta(hours=2), qtd=1, receita=100, margem=40),
        _item("shopee_1", "3", "minibikevickylab", LIBERACAO + timedelta(hours=3), qtd=1, receita=60, margem=5),
    ])
    pc = calcular_controle(itens, 3180, LIBERACAO, agora).por_canal.set_index("canal")
    assert pc.loc["amazon", "unidades"] == 2 and pc.loc["amazon", "margem_por_unid"] == 40.0
    assert pc.loc["shopee_1", "margem_pct"] == pytest.approx(8.3)
    assert pc.loc["TOTAL", "unidades"] == 3 and pc.loc["TOTAL", "margem"] == 85


def test_ignora_venda_antes_da_liberacao_e_conta_sem_custo():
    agora = LIBERACAO + timedelta(hours=5)
    itens = pd.DataFrame([
        _item("amazon", "1", "minibike-c1-dba", LIBERACAO - timedelta(hours=2)),
        _item("amazon", "2", "minibike-c1-dba", LIBERACAO + timedelta(hours=1), sem_custo=True),
    ])
    r = calcular_controle(itens, 3180, LIBERACAO, agora)
    assert r.unidades == 1 and r.itens_sem_custo == 1


def test_apos_24h_calcula_janela_das_ultimas_24h_e_faixa_poisson():
    agora = LIBERACAO + timedelta(hours=48)
    itens = pd.DataFrame(
        [_item("amazon", f"a{i}", "minibike-c1-dba", LIBERACAO + timedelta(hours=1 + i)) for i in range(10)]  # primeiras 24h
        + [_item("amazon", f"b{i}", "minibike-c1-dba", LIBERACAO + timedelta(hours=30 + i)) for i in range(10)]  # ultimas 24h
    )
    r = calcular_controle(itens, 3180, LIBERACAO, agora)
    assert r.ritmo_24h == 10 and r.ritmo_dia == pytest.approx(10)
    assert r.dias_estoque_min < r.dias_estoque < r.dias_estoque_max


def test_sem_vendas_nao_quebra():
    r = calcular_controle(pd.DataFrame(columns=["sku"]), 3180, LIBERACAO, LIBERACAO + timedelta(hours=3))
    assert r.unidades == 0 and r.dias_estoque is None and r.por_canal.empty
