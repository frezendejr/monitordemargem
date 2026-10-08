"""Indicadores de loja usados no dashboard que nao dependem do Streamlit
(testaveis isolados, mesmo padrao de minibike_controle.py):

- P.A. (pares por atendimento) = pares vendidos / pedidos. Cada pedido conta
  como 1 atendimento; a quantidade vem de margin_monitor_itens (pedidos
  gravados antes do rateio por item existir nao tem linha la, por isso o
  denominador tambem sai dos itens - senao o P.A. ficaria subestimado).
- Monitor de margem: itens vendidos por MENOS do que o custo da mercadoria
  (receita do item < CMV), com loja, venda, produto e horario.
"""

from __future__ import annotations

import pandas as pd

from minibike_controle import FUSO_BRASILIA


def calcular_pa(itens: pd.DataFrame) -> tuple[float | None, float, int]:
    """Retorna (pa, pares, atendimentos). pa = None sem nenhum pedido."""
    if itens.empty:
        return None, 0.0, 0
    pares = float(pd.to_numeric(itens["quantidade"], errors="coerce").fillna(0).sum())
    atendimentos = int(itens[["conta_tiny", "numero_pedido"]].drop_duplicates().shape[0])
    if not atendimentos:
        return None, pares, 0
    return pares / atendimentos, pares, atendimentos


def pa_por_grupo(itens: pd.DataFrame, coluna: str) -> pd.DataFrame:
    """P.A. agrupado por uma coluna (ex.: canal) - colunas: <coluna>, pares, atendimentos, pa."""
    if itens.empty:
        return pd.DataFrame(columns=[coluna, "pares", "atendimentos", "pa"])
    df = itens.assign(quantidade=pd.to_numeric(itens["quantidade"], errors="coerce").fillna(0))
    df = df.assign(_pedido=df["conta_tiny"].astype(str) + "|" + df["numero_pedido"].astype(str))
    r = df.groupby(coluna).agg(pares=("quantidade", "sum"), atendimentos=("_pedido", "nunique")).reset_index()
    r["pa"] = (r["pares"] / r["atendimentos"]).round(2)
    return r


def vendas_abaixo_custo(itens: pd.DataFrame) -> pd.DataFrame:
    """Itens com receita < CMV (so os com custo conhecido), do mais recente
    pro mais antigo. Adiciona `horario` (datetime em Brasilia) a partir de
    processado_em - o Tiny so expoe a DATA do pedido, entao o horario e o
    momento em que o monitor detectou/gravou a venda (ciclo de ~15 min)."""
    if itens.empty:
        return itens.assign(horario=pd.NaT, perda=0.0)
    cmv = pd.to_numeric(itens["cmv"], errors="coerce")
    receita = pd.to_numeric(itens["receita"], errors="coerce")
    sem_custo = itens["custo_ausente"].fillna(False).astype(bool)
    abaixo = itens[~sem_custo & (cmv > 0) & (receita < cmv)].copy()
    abaixo["perda"] = (cmv - receita)[abaixo.index].round(2)
    if "processado_em" in abaixo.columns:
        abaixo["horario"] = pd.to_datetime(abaixo["processado_em"], utc=True, errors="coerce").dt.tz_convert(FUSO_BRASILIA)
    else:
        abaixo["horario"] = pd.NaT
    return abaixo.sort_values("horario", ascending=False, na_position="last")
