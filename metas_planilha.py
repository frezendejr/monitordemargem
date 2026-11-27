"""Meta diaria da planilha "Metas Site" do time.

A planilha tem uma aba por mes ("Novembro 2026") com a meta de cada canal
dia a dia, incluindo as datas especiais (11.11, Black Friday, Cyber Monday,
feriados) que a sazonalidade por dia da semana do dashboard nao enxerga. O
dashboard usa esse desenho diario pra repartir a meta do mes de cada canal
entre os dias; o TOTAL do mes continua sendo o cadastrado no dashboard (aba
Visao geral, expander "Meta do mes").

Leitura pelo endpoint gviz do Google Sheets, que funciona sem login quando a
planilha esta compartilhada por link. O ID da planilha fica nos Secrets do
app (METAS_PLANILHA_ID), nunca no repositorio (o repo e publico).
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import requests

MESES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]
DIAS_SEMANA_PT = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]

# Nome do canal na planilha (minusculo, sem espacos nas pontas) -> canal no
# monitor. Canais da planilha que o monitor nao acompanha (Shein, NetShoes,
# Dafiti, Loja Integrada) ficam de fora.
CANAL_PLANILHA = {
    "meli conta 1": "meli_conta_1",
    "meli conta 2": "meli_conta_2",
    "meli conta 3": "meli_conta_3",
    "meli conta 4": "meli_conta_4",
    "shoppe 1": "shopee_1",
    "shopee 1": "shopee_1",
    "shoppe 2": "shopee_2",
    "shopee 2": "shopee_2",
    "amazon - inamorato": "amazon",
    "amazon": "amazon",
    "tiktok shop": "tiktok_shop",
    "tiktok": "tiktok_shop",
}

# Colunas A (dia do mes), B (dia da semana), C (canal), D (meta). Cada dia
# ocupa ~13 linhas (1 por canal + separador + total), 31 dias cabem com folga.
_INTERVALO = "A3:D600"


class PlanilhaIndisponivel(Exception):
    """A aba do mes nao pode ser lida ou nao e do mes pedido."""


def nome_aba(mes: date) -> str:
    return f"{MESES_PT[mes.month - 1]} {mes.year}"


def dias_do_mes(mes: date) -> list[date]:
    proximo = date(mes.year + 1, 1, 1) if mes.month == 12 else date(mes.year, mes.month + 1, 1)
    dias, d = [], mes.replace(day=1)
    while d < proximo:
        dias.append(d)
        d += timedelta(days=1)
    return dias


def interpretar_gviz(texto: str, mes: date) -> dict[str, dict[date, float]]:
    """Resposta do gviz (tqx=out:json) -> {canal: {dia: meta}}.

    Levanta PlanilhaIndisponivel quando a resposta nao e da aba do mes pedido:
    quando o nome da aba nao existe, o gviz devolve a PRIMEIRA aba da
    planilha sem erro nenhum - por isso o dia da semana de cada linha e
    conferido com o calendario do mes.
    """
    inicio, fim = texto.find("("), texto.rfind(")")
    if inicio < 0 or fim <= inicio:
        raise PlanilhaIndisponivel("resposta inesperada do Google Sheets (a planilha está compartilhada por link?)")
    try:
        dados = json.loads(texto[inicio + 1 : fim])
    except json.JSONDecodeError as e:
        raise PlanilhaIndisponivel(f"resposta inesperada do Google Sheets: {e}") from e
    if dados.get("status") != "ok":
        motivos = "; ".join(
            str(e.get("detailed_message") or e.get("message") or e.get("reason")) for e in dados.get("errors") or []
        )
        raise PlanilhaIndisponivel(motivos or "o Google Sheets devolveu erro")

    dias_validos = {d.day: d for d in dias_do_mes(mes)}
    metas: dict[str, dict[date, float]] = {}
    for linha in (dados.get("table") or {}).get("rows") or []:
        valores = [(c or {}).get("v") for c in (linha.get("c") or [])] + [None] * 4
        n_dia, semana, nome_canal, meta = valores[:4]
        canal = CANAL_PLANILHA.get(str(nome_canal or "").strip().lower())
        if canal is None or not isinstance(n_dia, (int, float)) or not isinstance(meta, (int, float)):
            continue  # total do dia, separador, resumo no rodape ou canal que o monitor nao acompanha
        dia = dias_validos.get(int(n_dia))
        if dia is None:
            continue
        esperado = DIAS_SEMANA_PT[dia.weekday()]
        if str(semana or "").strip().lower() != esperado:
            raise PlanilhaIndisponivel(
                f"aba '{nome_aba(mes)}' não encontrada na planilha "
                f"(o dia {dia.day} da aba lida não é {esperado})"
            )
        metas.setdefault(canal, {})
        metas[canal][dia] = metas[canal].get(dia, 0.0) + float(meta)

    if not metas:
        raise PlanilhaIndisponivel(f"nenhuma meta diária reconhecida na aba '{nome_aba(mes)}'")
    return metas


def baixar_metas_mes(planilha_id: str, mes: date, timeout: float = 15) -> dict[str, dict[date, float]]:
    """{canal: {dia: meta}} da aba do mes. Levanta PlanilhaIndisponivel."""
    try:
        resp = requests.get(
            f"https://docs.google.com/spreadsheets/d/{planilha_id}/gviz/tq",
            params={"tqx": "out:json", "sheet": nome_aba(mes), "range": _INTERVALO, "headers": "0"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise PlanilhaIndisponivel(f"falha ao ler a planilha: {e}") from e
    resp.encoding = "utf-8"
    return interpretar_gviz(resp.text, mes)


def pesos_por_dia(
    dias_mes: list[date], indices_canal: dict[int, float] | None, metas_planilha_canal: dict[date, float] | None = None
) -> dict[date, float]:
    """Fracao da meta do mes de UM canal que cai em cada dia (soma 1).

    Segue o desenho da planilha quando ela tem a meta do canal em todos os
    dias do mes (e o total e maior que zero); senao, a sazonalidade por dia
    da semana do canal (`indices_canal`: 0=segunda ... 6=domingo).
    """
    if metas_planilha_canal and all(d in metas_planilha_canal for d in dias_mes):
        total = sum(metas_planilha_canal[d] for d in dias_mes)
        if total > 0:
            return {d: metas_planilha_canal[d] / total for d in dias_mes}

    indices = indices_canal or {i: 1.0 for i in range(7)}
    soma = sum(indices[d.weekday()] for d in dias_mes)
    if not soma:
        return {d: 0.0 for d in dias_mes}
    return {d: indices[d.weekday()] / soma for d in dias_mes}


def total_por_canal(metas_planilha: dict[str, dict[date, float]]) -> dict[str, float]:
    return {canal: round(sum(por_dia.values()), 2) for canal, por_dia in metas_planilha.items()}
