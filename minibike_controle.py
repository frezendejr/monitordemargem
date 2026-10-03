"""Controle de produto (minibike): unidades e margem por marketplace + dias de
estoque. Calculo isolado do Streamlit pra ser testavel.

Por que o ritmo e por HORA DE EXPOSICAO e nao por dia calendario: o estoque
(3.180 pecas, recebido em 02/10) so foi liberado pra venda as 20h, entao
02/10 teve ~4h de venda e "vendas/dia" calendario subestimaria o ritmo real
(e 03/10, o primeiro dia cheio, ainda esta em andamento). Ritmo/dia =
unidades / horas desde a liberacao x 24.

Hora da venda = processado_em (quando o monitor gravou o pedido, ~5 min da
venda): a tabela so guarda a DATA do pedido, nao a hora.

Amostra pequena: com n vendas o erro tipico de contagem e ~sqrt(n) (Poisson);
a faixa de dias de estoque usa n +/- 1,96*sqrt(n) pra nao passar falsa
precisao enquanto houver poucas dezenas de vendas.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

FUSO_BRASILIA = timezone(timedelta(hours=-3))
PADROES_SKU = ("minibike", "pj198")  # SKUs do produto (variacoes por anuncio/kit)


@dataclass
class ControleMinibike:
    por_canal: pd.DataFrame  # ja com linha TOTAL no fim
    por_dia: pd.DataFrame
    unidades: float
    estoque_inicial: float
    estoque_atual: float
    horas_expostas: float
    ritmo_dia: float | None  # media desde a liberacao, ajustada por horas expostas
    ritmo_24h: float | None  # so quando ja ha 24h completas de venda
    dias_estoque: float | None
    dias_estoque_min: float | None  # ritmo alto (limite superior de Poisson)
    dias_estoque_max: float | None  # ritmo baixo (limite inferior de Poisson)
    esgota_em: datetime | None
    itens_sem_custo: int


def filtrar_produto(itens: pd.DataFrame, padroes: tuple[str, ...] = PADROES_SKU) -> pd.DataFrame:
    if itens.empty or "sku" not in itens.columns:
        return itens.iloc[0:0].copy()
    regex = "|".join(re.escape(p) for p in padroes)
    return itens[itens["sku"].astype(str).str.contains(regex, case=False, regex=True, na=False)].copy()


def _horas_no_dia(dia, inicio: datetime, fim: datetime) -> float:
    """Horas do `dia` (date) dentro da janela [inicio, fim] - exposicao real de venda."""
    ini_dia = datetime(dia.year, dia.month, dia.day, tzinfo=FUSO_BRASILIA)
    a, b = max(ini_dia, inicio), min(ini_dia + timedelta(days=1), fim)
    return max((b - a).total_seconds() / 3600, 0.0)


def calcular_controle(
    itens: pd.DataFrame, estoque_inicial: float, liberacao: datetime, agora: datetime
) -> ControleMinibike:
    df = filtrar_produto(itens)
    for col in ("quantidade", "receita", "cmv", "margem_contribuicao"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0) if col in df.columns else 0.0
    if "processado_em" not in df.columns:
        df["processado_em"] = pd.NaT
    df["quando"] = pd.to_datetime(df["processado_em"], utc=True, errors="coerce").dt.tz_convert(FUSO_BRASILIA)
    df = df[df["quando"].notna() & (df["quando"] >= liberacao) & (df["quando"] <= agora)]
    if "custo_ausente" not in df.columns:
        df["custo_ausente"] = False

    if df.empty:
        por_canal = pd.DataFrame(
            columns=["canal", "pedidos", "unidades", "receita", "cmv", "margem", "margem_pct", "margem_por_unid"]
        )
    else:
        por_canal = (
            df.groupby("canal")
            .agg(
                pedidos=("numero_pedido", "nunique"),
                unidades=("quantidade", "sum"),
                receita=("receita", "sum"),
                cmv=("cmv", "sum"),
                margem=("margem_contribuicao", "sum"),
            )
            .reset_index()
        )
        total = {c: por_canal[c].sum() for c in ("pedidos", "unidades", "receita", "cmv", "margem")}
        por_canal = pd.concat([por_canal, pd.DataFrame([{"canal": "TOTAL", **total}])], ignore_index=True)
        por_canal["margem_pct"] = por_canal.apply(
            lambda r: round(r["margem"] / r["receita"] * 100, 1) if r["receita"] else 0.0, axis=1
        )
        por_canal["margem_por_unid"] = por_canal.apply(
            lambda r: round(r["margem"] / r["unidades"], 2) if r["unidades"] else 0.0, axis=1
        )

    unidades = float(df["quantidade"].sum())
    horas = max((agora - liberacao).total_seconds() / 3600, 0.0)

    por_dia_rows = []
    if not df.empty:
        for dia, g in df.groupby(df["quando"].dt.date):
            h = _horas_no_dia(dia, liberacao, agora)
            u = float(g["quantidade"].sum())
            por_dia_rows.append(
                {"dia": dia, "unidades": u, "horas_expostas": round(h, 1), "equiv_dia": round(u / h * 24, 1) if h else None}
            )
    por_dia = pd.DataFrame(por_dia_rows, columns=["dia", "unidades", "horas_expostas", "equiv_dia"])

    estoque_atual = estoque_inicial - unidades
    ritmo = unidades / horas * 24 if horas > 0 and unidades > 0 else None
    ritmo_24h = None
    if horas >= 24:
        ritmo_24h = float(df.loc[df["quando"] > agora - timedelta(hours=24), "quantidade"].sum())

    dias = dmin = dmax = esgota = None
    if ritmo:
        margem_erro = 1.96 * math.sqrt(unidades)
        alto = (unidades + margem_erro) / horas * 24
        baixo = max(unidades - margem_erro, 0.0) / horas * 24
        dias = estoque_atual / ritmo
        dmin = estoque_atual / alto
        dmax = estoque_atual / baixo if baixo > 0 else None
        esgota = agora + timedelta(days=dias)

    sem_custo = int(df["custo_ausente"].astype(bool).sum()) if not df.empty else 0
    return ControleMinibike(
        por_canal=por_canal, por_dia=por_dia, unidades=unidades, estoque_inicial=estoque_inicial,
        estoque_atual=estoque_atual, horas_expostas=horas, ritmo_dia=ritmo, ritmo_24h=ritmo_24h,
        dias_estoque=dias, dias_estoque_min=dmin, dias_estoque_max=dmax, esgota_em=esgota,
        itens_sem_custo=sem_custo,
    )
