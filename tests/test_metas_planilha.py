import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from metas_planilha import (
    DIAS_SEMANA_PT,
    PlanilhaIndisponivel,
    dias_do_mes,
    interpretar_gviz,
    nome_aba,
    pesos_por_dia,
    total_por_canal,
)

NOVEMBRO = date(2026, 11, 1)

# Meta diaria do Meli Conta 1 na aba "Novembro 2026" (soma R$ 101.000, igual
# a meta do mes cadastrada no monitor). Dia 27 = Black Friday, dia 30 = Cyber Monday.
META_MELI_1_NOV = [
    2930, 2880, 4050, 3750, 3220, 3060, 2630, 2930, 3390, 4050,
    4500, 3220, 3060, 2630, 2640, 3390, 4050, 3750, 3220, 2750,
    2630, 2930, 3390, 4050, 3750, 3220, 4590, 3030, 3220, 4090,
]


def _resposta_gviz(linhas, status="ok"):
    """Monta o texto que o gviz devolve com tqx=out:json."""
    rows = [{"c": [None if v is None else {"v": v} for v in linha]} for linha in linhas]
    corpo = {"version": "0.6", "reqId": "0", "status": status, "table": {"cols": [], "rows": rows}}
    return "/*O_o*/\ngoogle.visualization.Query.setResponse(" + json.dumps(corpo) + ");"


def _linhas_mes(mes, metas_por_nome):
    """Linhas no formato da aba: dia, dia da semana, canal, meta + total do dia."""
    linhas = []
    for d in dias_do_mes(mes):
        semana = DIAS_SEMANA_PT[d.weekday()]
        total = 0.0
        for nome, valores in metas_por_nome.items():
            linhas.append([d.day, semana, nome, valores[d.day - 1]])
            total += valores[d.day - 1]
        linhas.append([d.day, semana, None, total])  # linha de total do dia
    return linhas


def test_nome_aba():
    assert nome_aba(NOVEMBRO) == "Novembro 2026"
    assert nome_aba(date(2027, 3, 1)) == "Março 2027"


def test_interpretar_gviz_mapeia_canais_e_ignora_os_que_o_monitor_nao_tem():
    linhas = _linhas_mes(
        NOVEMBRO,
        {
            "Meli Conta 1": META_MELI_1_NOV,
            "Shoppe 1": [100.0] * 30,
            "Amazon - Inamorato": [50.0] * 30,
            "NetShoes": [30.0] * 30,
            "Shein": [0.0] * 30,
        },
    )
    linhas.append(["", "TOTAL ", "SHOPEE 1", None])  # resumo no rodape da aba

    metas = interpretar_gviz(_resposta_gviz(linhas), NOVEMBRO)

    assert set(metas) == {"meli_conta_1", "shopee_1", "amazon"}
    assert metas["meli_conta_1"][date(2026, 11, 27)] == 4590
    assert total_por_canal(metas)["meli_conta_1"] == 101000


def test_aba_de_outro_mes_e_rejeitada():
    # Aba de dezembro ainda nao existe -> o gviz devolve a 1a aba (marco/2026,
    # que comeca num domingo). 1/12/2026 e terca: tem que recusar.
    linhas = [[1, "Domingo", "Meli Conta 1", 1612.90], [1, "Domingo", "Meli Conta 2", 1612.90]]
    with pytest.raises(PlanilhaIndisponivel, match="Dezembro 2026"):
        interpretar_gviz(_resposta_gviz(linhas), date(2026, 12, 1))


def test_resposta_com_erro_ou_sem_meta():
    with pytest.raises(PlanilhaIndisponivel):
        interpretar_gviz(_resposta_gviz([], status="error"), NOVEMBRO)
    with pytest.raises(PlanilhaIndisponivel):
        interpretar_gviz("<html>login</html>", NOVEMBRO)
    with pytest.raises(PlanilhaIndisponivel):
        interpretar_gviz(_resposta_gviz([[1, "domingo", "Shein", 0.0]]), NOVEMBRO)


def test_pesos_da_planilha_reproduzem_a_meta_do_dia():
    dias = dias_do_mes(NOVEMBRO)
    planilha = {d: float(v) for d, v in zip(dias, META_MELI_1_NOV)}

    pesos = pesos_por_dia(dias, {i: 1.0 for i in range(7)}, planilha)

    assert sum(pesos.values()) == pytest.approx(1.0)
    assert 101000 * pesos[date(2026, 11, 27)] == pytest.approx(4590)
    assert 101000 * pesos[date(2026, 11, 30)] == pytest.approx(4090)


def test_pesos_caem_na_sazonalidade_sem_planilha_ou_com_planilha_incompleta():
    dias = dias_do_mes(NOVEMBRO)
    indices = {0: 1.2, 1: 1.1, 2: 1.0, 3: 1.0, 4: 0.9, 5: 0.8, 6: 1.0}
    soma = sum(indices[d.weekday()] for d in dias)

    sem_planilha = pesos_por_dia(dias, indices, None)
    incompleta = pesos_por_dia(dias, indices, {dias[0]: 500.0})

    for pesos in (sem_planilha, incompleta):
        assert sum(pesos.values()) == pytest.approx(1.0)
        assert pesos[date(2026, 11, 27)] == pytest.approx(0.9 / soma)


def test_pesos_sem_indice_dividem_igual():
    dias = dias_do_mes(NOVEMBRO)
    pesos = pesos_por_dia(dias, None, None)
    assert pesos[dias[0]] == pytest.approx(1 / 30)
