"""Dedupe e checkpoint via Supabase, pra rodar o monitor num ambiente sem
disco persistente (GitHub Actions). Espelha a interface de storage.py
(ja_processado/obter_checkpoint/salvar_checkpoint), mas sem estado local -
cada chamada e uma requisicao HTTP.

Usa a mesma tabela margin_monitor_pedidos que ja alimenta o dashboard como
fonte de dedupe (se o pedido ja foi gravado la, ja foi processado) - evita
manter uma tabela de dedupe separada e redundante.
"""

from __future__ import annotations

import os

import requests


def _headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _base_url() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/")


def ja_processado(conta_tiny: str, numero_pedido: str) -> bool:
    resp = requests.get(
        f"{_base_url()}/rest/v1/margin_monitor_pedidos",
        params={"conta_tiny": f"eq.{conta_tiny}", "numero_pedido": f"eq.{numero_pedido}", "select": "numero_pedido"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def obter_checkpoint(conta_tiny: str) -> str | None:
    resp = requests.get(
        f"{_base_url()}/rest/v1/margin_monitor_checkpoint",
        params={"conta_tiny": f"eq.{conta_tiny}", "select": "ultima_busca_iso"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    linhas = resp.json()
    return linhas[0]["ultima_busca_iso"] if linhas else None


def salvar_checkpoint(conta_tiny: str, quando_iso: str) -> None:
    resp = requests.post(
        f"{_base_url()}/rest/v1/margin_monitor_checkpoint?on_conflict=conta_tiny",
        json={"conta_tiny": conta_tiny, "ultima_busca_iso": quando_iso},
        headers={**_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
        timeout=15,
    )
    resp.raise_for_status()
