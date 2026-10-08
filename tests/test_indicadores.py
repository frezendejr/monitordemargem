import pandas as pd
import pytest

from indicadores import calcular_pa, pa_por_grupo, vendas_abaixo_custo


def _item(conta, pedido, sku, canal="shopee_1", qtd=1, receita=100.0, cmv=50.0, sem_custo=False,
          processado_em="2026-10-08T15:30:00+00:00"):
    return {"conta_tiny": conta, "numero_pedido": pedido, "sku": sku, "canal": canal, "quantidade": qtd,
            "receita": receita, "cmv": cmv, "custo_ausente": sem_custo, "processado_em": processado_em}


def test_pa_soma_pares_e_divide_por_pedidos_distintos():
    itens = pd.DataFrame([
        _item("a", "1", "91603901", qtd=2),
        _item("a", "1", "91603902", qtd=1),
        _item("a", "2", "91603901", qtd=1),
        _item("b", "1", "91603901", qtd=1),  # mesmo numero, outra conta Tiny = outro atendimento
    ])
    pa, pares, atendimentos = calcular_pa(itens)
    assert (pares, atendimentos) == (5, 3)
    assert pa == pytest.approx(5 / 3)


def test_pa_sem_itens():
    assert calcular_pa(pd.DataFrame()) == (None, 0.0, 0)


def test_pa_por_canal():
    itens = pd.DataFrame([
        _item("a", "1", "x", canal="shopee_1", qtd=3),
        _item("a", "2", "x", canal="shopee_1", qtd=1),
        _item("a", "3", "x", canal="amazon", qtd=1),
    ])
    r = pa_por_grupo(itens, "canal").set_index("canal")
    assert r.loc["shopee_1", "pa"] == 2.0 and r.loc["amazon", "pa"] == 1.0


def test_abaixo_custo_ignora_sem_custo_e_ordena_pelo_mais_recente():
    itens = pd.DataFrame([
        _item("a", "1", "ok", receita=100, cmv=50),
        _item("a", "2", "prejuizo_antigo", receita=40, cmv=50, processado_em="2026-10-08T12:00:00+00:00"),
        _item("a", "3", "prejuizo_novo", receita=30, cmv=50, processado_em="2026-10-08T18:10:00+00:00"),
        _item("a", "4", "sem_custo", receita=10, cmv=50, sem_custo=True),
    ])
    r = vendas_abaixo_custo(itens)
    assert list(r["sku"]) == ["prejuizo_novo", "prejuizo_antigo"]
    assert list(r["perda"]) == [20.0, 10.0]
    assert r.iloc[0]["horario"].strftime("%d/%m %H:%M") == "08/10 15:10"  # horario de Brasilia


def test_abaixo_custo_vazio():
    assert vendas_abaixo_custo(pd.DataFrame()).empty
