"""Recalcula a margem dos pedidos JA GRAVADOS de um mes quando muda a aliquota
de imposto ou quando o custo de algum SKU estava errado.

O monitor so calcula a margem na hora em que o pedido entra - trocar o
imposto_pct do config.yaml ou corrigir um custo no dashboard so vale pros
pedidos NOVOS. Este script corrige o que ja esta no Supabase:

- Imposto: passa a ser `--imposto`% do VALOR DA VENDA (valor_venda, a nota) -
  inclusive no Mercado Livre e na Shopee, em que antes o imposto era calculado
  sobre a receita liquida (o repasse).
- CMV: nos SKUs de `--skus`, CMV = quantidade x custo cadastrado HOJE em
  margin_monitor_custos. Item com custo_ausente fica de fora (quem corrige esse
  e o recalcular_custo_pendente do monitor, senao o CMV seria descontado 2x).

A margem do pedido muda so pela diferenca (imposto novo - antigo, CMV novo -
antigo); o resto (receita, comissao, frete, Ads) fica como foi gravado. Os itens
do pedido sao reajustados pra somar exatamente a margem do pedido, rateados
pela receita de cada item (mesmo criterio do margin_engine).

Pode rodar quantas vezes quiser: rodar de novo com os mesmos parametros nao
muda nada. Sem --aplicar so mostra o resumo do que mudaria.

Uso:
    python recalcular_margens.py --mes 11/2026 --imposto 7.5 \\
        --skus lixeirapreto-1076,kit2lixeirapreto-1076 [--aplicar]

Precisa de SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY (no .env da VM ou nos
secrets do GitHub - ver .github/workflows/recalcular_margens.yml).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

import requests
from dotenv import load_dotenv

_PAGINA = 1000
_LOTE_GRAVACAO = 500
_TOLERANCIA = 0.005


def _num(valor) -> float:
    return float(valor) if valor is not None else 0.0


def recalcular(
    pedidos: list[dict], itens: list[dict], imposto_pct: float, custo_por_sku: dict[str, float]
) -> tuple[list[dict], list[dict], dict]:
    """Conta pura (sem rede). Devolve (pedidos_alterados, itens_alterados, resumo),
    ja no formato de upsert do Supabase."""
    itens_por_pedido: dict[tuple, list[dict]] = defaultdict(list)
    for item in itens:
        itens_por_pedido[(item["conta_tiny"], str(item["numero_pedido"]))].append(item)

    resumo = {
        "pedidos": len(pedidos),
        "pedidos_alterados": 0,
        "itens_alterados": 0,
        "sem_detalhe_ignorados": 0,
        "imposto_antes": 0.0,
        "imposto_depois": 0.0,
        "cmv_antes": 0.0,
        "cmv_depois": 0.0,
        "margem_antes": 0.0,
        "margem_depois": 0.0,
        "margem_por_canal": defaultdict(lambda: [0.0, 0.0]),
    }
    pedidos_alterados: list[dict] = []
    itens_alterados: list[dict] = []

    for pedido in pedidos:
        chave = (pedido["conta_tiny"], str(pedido["numero_pedido"]))
        margem_antiga = _num(pedido["margem_contribuicao"])
        resumo["margem_antes"] += margem_antiga
        resumo["margem_por_canal"][pedido["canal"]][0] += margem_antiga

        if pedido.get("imposto") is None:
            # Pedido antigo, gravado antes do detalhamento (sem imposto/valor_venda
            # separados) - nao da pra saber o que ja foi descontado. Fica como esta.
            resumo["sem_detalhe_ignorados"] += 1
            resumo["margem_depois"] += margem_antiga
            resumo["margem_por_canal"][pedido["canal"]][1] += margem_antiga
            continue

        receita = _num(pedido["receita"])
        base_venda = _num(pedido["valor_venda"]) if pedido.get("valor_venda") is not None else receita
        imposto_antigo = _num(pedido["imposto"])
        imposto_novo = round(base_venda * imposto_pct / 100, 2)
        delta_imposto = imposto_novo - imposto_antigo

        itens_do_pedido = itens_por_pedido.get(chave, [])
        delta_cmv_por_sku: dict[str, tuple[float, float]] = {}
        for item in itens_do_pedido:
            if item["sku"] in custo_por_sku and not item.get("custo_ausente"):
                cmv_novo_item = round(_num(item["quantidade"]) * custo_por_sku[item["sku"]], 2)
                delta = cmv_novo_item - _num(item["cmv"])
                if abs(delta) >= _TOLERANCIA:
                    delta_cmv_por_sku[item["sku"]] = (cmv_novo_item, delta)
        delta_cmv = sum(delta for _, delta in delta_cmv_por_sku.values())

        cmv_antigo = _num(pedido.get("cmv"))
        resumo["imposto_antes"] += imposto_antigo
        resumo["cmv_antes"] += cmv_antigo

        if abs(delta_imposto) < _TOLERANCIA and not delta_cmv_por_sku:
            resumo["imposto_depois"] += imposto_antigo
            resumo["cmv_depois"] += cmv_antigo
            resumo["margem_depois"] += margem_antiga
            resumo["margem_por_canal"][pedido["canal"]][1] += margem_antiga
            continue

        margem_nova = round(margem_antiga - delta_imposto - delta_cmv, 2)
        cmv_novo = round(cmv_antigo + delta_cmv, 2)
        resumo["imposto_depois"] += imposto_novo
        resumo["cmv_depois"] += cmv_novo
        resumo["margem_depois"] += margem_nova
        resumo["margem_por_canal"][pedido["canal"]][1] += margem_nova
        resumo["pedidos_alterados"] += 1

        pedidos_alterados.append(
            {
                "conta_tiny": pedido["conta_tiny"],
                "numero_pedido": pedido["numero_pedido"],
                "canal": pedido["canal"],
                "imposto": imposto_novo,
                "cmv": cmv_novo,
                "margem_contribuicao": margem_nova,
                "margem_pct": round(margem_nova / receita * 100, 2) if receita else 0.0,
            }
        )

        if not itens_do_pedido:
            continue

        # Itens: aplica o CMV novo de cada um e rateia o resto da diferenca
        # (imposto + o que ja nao batia entre itens e pedido) pela receita.
        margem_parcial = {}
        for item in itens_do_pedido:
            delta_item = delta_cmv_por_sku.get(item["sku"], (None, 0.0))[1]
            margem_parcial[item["sku"]] = _num(item["margem_contribuicao"]) - delta_item
        residuo = margem_nova - sum(margem_parcial.values())
        receita_itens = sum(_num(item["receita"]) for item in itens_do_pedido)

        for item in itens_do_pedido:
            peso = _num(item["receita"]) / receita_itens if receita_itens else 1 / len(itens_do_pedido)
            margem_item = round(margem_parcial[item["sku"]] + residuo * peso, 2)
            cmv_item = delta_cmv_por_sku.get(item["sku"], (_num(item["cmv"]), 0.0))[0]
            receita_item = _num(item["receita"])
            itens_alterados.append(
                {
                    "conta_tiny": item["conta_tiny"],
                    "numero_pedido": item["numero_pedido"],
                    "sku": item["sku"],
                    "canal": item["canal"],
                    "cmv": round(cmv_item, 2),
                    "margem_contribuicao": margem_item,
                    "margem_pct": round(margem_item / receita_item * 100, 2) if receita_item else 0.0,
                }
            )
            resumo["itens_alterados"] += 1

    return pedidos_alterados, itens_alterados, resumo


# --------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------


def _headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _url(tabela: str) -> str:
    return f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{tabela}"


def _ler_tudo(tabela: str, params: dict) -> list[dict]:
    linhas: list[dict] = []
    inicio = 0
    while True:
        resp = requests.get(
            _url(tabela),
            params=params,
            headers={**_headers(), "Range": f"{inicio}-{inicio + _PAGINA - 1}"},
            timeout=60,
        )
        resp.raise_for_status()
        pagina = resp.json()
        linhas.extend(pagina)
        if len(pagina) < _PAGINA:
            return linhas
        inicio += _PAGINA


def _ler_custos(skus: list[str]) -> dict[str, float]:
    if not skus:
        return {}
    lista = ",".join('"' + sku.replace('"', "") + '"' for sku in skus)
    linhas = _ler_tudo("margin_monitor_custos", {"select": "sku,custo", "sku": f"in.({lista})"})
    return {linha["sku"]: float(linha["custo"]) for linha in linhas if linha.get("custo")}


def _gravar(tabela: str, on_conflict: str, linhas: list[dict]) -> None:
    for i in range(0, len(linhas), _LOTE_GRAVACAO):
        resp = requests.post(
            f"{_url(tabela)}?on_conflict={on_conflict}",
            json=linhas[i : i + _LOTE_GRAVACAO],
            headers={
                **_headers(),
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            timeout=60,
        )
        resp.raise_for_status()


def _brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mes", required=True, help="mes dos pedidos, MM/AAAA (ex.: 11/2026)")
    parser.add_argument("--imposto", required=True, type=float, help="aliquota em %% sobre o valor da venda")
    parser.add_argument("--skus", default="", help="SKUs com custo corrigido, separados por virgula")
    parser.add_argument("--aplicar", action="store_true", help="grava no Supabase (sem isso so simula)")
    args = parser.parse_args()

    if not re.fullmatch(r"\d{2}/\d{4}", args.mes):
        parser.error("--mes precisa ser MM/AAAA, ex.: 11/2026")
    if not 0 <= args.imposto <= 30:
        parser.error("--imposto fora do esperado (0 a 30)")

    skus = [s.strip() for s in args.skus.split(",") if s.strip()]
    custos = _ler_custos(skus)
    sem_custo = [s for s in skus if s not in custos]
    if sem_custo:
        print(f"ERRO: SKU(s) sem custo cadastrado em margin_monitor_custos: {', '.join(sem_custo)}")
        return 1

    filtro_mes = {"data_pedido": f"like.*/{args.mes}"}
    pedidos = _ler_tudo(
        "margin_monitor_pedidos",
        {"select": "conta_tiny,numero_pedido,canal,receita,valor_venda,cmv,imposto,margem_contribuicao", **filtro_mes},
    )
    itens = _ler_tudo(
        "margin_monitor_itens",
        {"select": "conta_tiny,numero_pedido,sku,canal,quantidade,receita,cmv,margem_contribuicao,custo_ausente", **filtro_mes},
    )

    pedidos_alterados, itens_alterados, resumo = recalcular(pedidos, itens, args.imposto, custos)

    print(f"Mes {args.mes} - imposto {args.imposto}% sobre o valor da venda")
    if custos:
        print("Custos usados: " + ", ".join(f"{sku}={custo:.2f}" for sku, custo in sorted(custos.items())))
    print(f"Pedidos no mes: {resumo['pedidos']} | alterados: {resumo['pedidos_alterados']} | itens alterados: {resumo['itens_alterados']}")
    if resumo["sem_detalhe_ignorados"]:
        print(f"Pedidos sem detalhamento (ignorados): {resumo['sem_detalhe_ignorados']}")
    print(f"Imposto: {_brl(resumo['imposto_antes'])} -> {_brl(resumo['imposto_depois'])}")
    print(f"CMV:     {_brl(resumo['cmv_antes'])} -> {_brl(resumo['cmv_depois'])}")
    print(f"Margem:  {_brl(resumo['margem_antes'])} -> {_brl(resumo['margem_depois'])}")
    for canal, (antes, depois) in sorted(resumo["margem_por_canal"].items()):
        print(f"  {canal}: {_brl(antes)} -> {_brl(depois)}")

    if not args.aplicar:
        print("SIMULACAO - nada foi gravado. Rode com --aplicar pra gravar.")
        return 0

    _gravar("margin_monitor_pedidos", "conta_tiny,numero_pedido", pedidos_alterados)
    _gravar("margin_monitor_itens", "conta_tiny,numero_pedido,sku", itens_alterados)
    print(f"GRAVADO: {len(pedidos_alterados)} pedido(s) e {len(itens_alterados)} item(ns) atualizados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
