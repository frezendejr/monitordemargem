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
    payload = [
        {
            "conta_tiny": conta_tiny,
            "numero_pedido": resultado.numero_pedido,
            "sku": item.sku,
            "canal": resultado.canal,
            "data_pedido": resultado.data_pedido,
            "quantidade": item.quantidade,
            "receita": item.receita,
            "cmv": item.cmv,
            "margem_contribuicao": item.margem_contribuicao,
            "margem_pct": item.margem_pct,
            "custo_ausente": item.custo_ausente,
            "anuncio_id": anuncios.get(item.sku),
        }
        for item in resultado.itens
    ]
    resp = requests.post(
        f"{_base_url()}/rest/v1/margin_monitor_itens?on_conflict=conta_tiny,numero_pedido,sku",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if not resp.ok:
        raise SupabaseError(f"Falha ao gravar itens no Supabase: {resp.status_code} {resp.text}")
