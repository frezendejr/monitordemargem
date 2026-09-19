"""Grava os resultados de margem no Supabase (mesmo projeto do AmoApp), so
para alimentar o dashboard. O SQLite local (storage.py) continua sendo a
fonte de verdade operacional (dedupe/checkpoint) - isso aqui e so uma copia
de relatorio, e uma falha de rede/credencial aqui NUNCA deve derrubar o
monitor (ver chamada em monitor.py, sempre dentro de try/except).

Usa a API REST do Supabase (PostgREST) direto via requests, sem adicionar a
dependencia supabase-py (mais pesada do que precisamos so pra inserir/
atualizar 2 tabelas).
"""

from __future__ import annotations

import os

import requests

from margin_engine import ResultadoMargem


class SupabaseError(RuntimeError):
    """Erro ao gravar no Supabase."""


def _headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def _base_url() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/")


def enviar_pedido(conta_tiny: str, resultado: ResultadoMargem, alertado: bool, valor_venda: float | None = None) -> None:
    payload = {
        "conta_tiny": conta_tiny,
        "numero_pedido": resultado.numero_pedido,
        "canal": resultado.canal,
        "data_pedido": resultado.data_pedido,
        "receita": resultado.receita,
        "valor_venda": valor_venda if valor_venda is not None else resultado.receita,
        "cmv": resultado.cmv,
        "imposto": resultado.imposto,
        "comissao": resultado.comissao,
        "frete": resultado.frete,
        "ads": resultado.ads,
        "margem_contribuicao": resultado.margem_contribuicao,
        "margem_pct": resultado.margem_pct,
        "custo_ausente": resultado.custo_ausente,
        "alertado": alertado,
    }
    resp = requests.post(
        f"{_base_url()}/rest/v1/margin_monitor_pedidos?on_conflict=conta_tiny,numero_pedido",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if not resp.ok:
        raise SupabaseError(f"Falha ao gravar pedido no Supabase: {resp.status_code} {resp.text}")


def enviar_itens(conta_tiny: str, resultado: ResultadoMargem, anuncios: dict[str, str] | None = None) -> None:
    if not resultado.itens:
        return

    anuncios = anuncios or {}

    # Um pedido pode ter o mesmo SKU em 2 linhas separadas (ex.: mesmo
    # produto adicionado 2x no carrinho) - o upsert do Postgres nao aceita
    # 2 linhas com a mesma chave de conflito dentro do MESMO comando
    # ("ON CONFLICT DO UPDATE command cannot affect row a second time"),
    # entao soma as linhas do mesmo sku ANTES de montar o payload.
    por_sku: dict[str, dict] = {}
    for item in resultado.itens:
        acc = por_sku.setdefault(
            item.sku,
            {"quantidade": 0.0, "receita": 0.0, "cmv": 0.0, "margem_contribuicao": 0.0, "custo_ausente": False},
        )
        acc["quantidade"] += item.quantidade
        acc["receita"] += item.receita
        acc["cmv"] += item.cmv
        acc["margem_contribuicao"] += item.margem_contribuicao
        acc["custo_ausente"] = acc["custo_ausente"] or item.custo_ausente

    payload = [
        {
            "conta_tiny": conta_tiny,
            "numero_pedido": resultado.numero_pedido,
            "sku": sku,
            "canal": resultado.canal,
            "data_pedido": resultado.data_pedido,
            "quantidade": acc["quantidade"],
            "receita": round(acc["receita"], 2),
            "cmv": round(acc["cmv"], 2),
            "margem_contribuicao": round(acc["margem_contribuicao"], 2),
            "margem_pct": round(acc["margem_contribuicao"] / acc["receita"] * 100, 2) if acc["receita"] else 0.0,
            "custo_ausente": acc["custo_ausente"],
            "anuncio_id": anuncios.get(sku),
        }
        for sku, acc in por_sku.items()
    ]
    resp = requests.post(
        f"{_base_url()}/rest/v1/margin_monitor_itens?on_conflict=conta_tiny,numero_pedido,sku",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if not resp.ok:
        raise SupabaseError(f"Falha ao gravar itens no Supabase: {resp.status_code} {resp.text}")
