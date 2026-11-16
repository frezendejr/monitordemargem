"""Snapshot dos anuncios PAUSADOS de cada marketplace - alimenta a aba
"Anuncios pausados" do dashboard (ver dashboard.py). Grava em
margin_monitor_anuncios_pausados.

- Mercado Livre (4 contas): status "paused" da API (o sub_status diz o
  motivo, ex.: "out_of_stock" = pausado automatico por falta de estoque).
- Shopee (2 lojas): item_status "UNLIST" (deslistado no Seller Center).
- Amazon e TikTok Shop ficam de fora: o projeto nao integra a API de
  catalogo desses canais (so le os pedidos via Tiny).

E um snapshot, nao historico: a cada rodada as linhas de cada conta sao
substituidas pelas atuais (anuncio reativado some da lista). Conta que
falhar na API mantem o snapshot anterior em vez de ficar zerada.

Uso: python anuncios_pausados_job.py   (sugestao: 1x por hora na VM)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from ml_client import MLApiError, MLClient
from shopee_client import ShopeeApiError, ShopeeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("anuncios_pausados_job")

CONTAS_ML = ["meli_conta_1", "meli_conta_2", "meli_conta_3", "meli_conta_4"]
LOJAS_SHOPEE = ["shopee_1", "shopee_2"]
TABELA = "margin_monitor_anuncios_pausados"

# Motivos de pausa do ML (sub_status) em portugues - os nao mapeados aparecem crus.
MOTIVOS_ML = {
    "out_of_stock": "Sem estoque",
    "deleted": "Excluído",
    "forbidden": "Proibido",
    "freezed": "Congelado",
    "suspended": "Suspenso",
    "waiting_for_patch": "Aguardando correção",
    "held": "Retido",
    "picture_download_pending": "Imagem pendente",
    "pending_documentation": "Documentação pendente",
    "warning": "Com alerta",
    "moderation_penalty": "Penalidade de moderação",
}


def _sku_ml(anuncio: dict) -> str | None:
    if anuncio.get("seller_custom_field"):
        return anuncio["seller_custom_field"]
    for attr in anuncio.get("attributes") or []:
        if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
            return attr["value_name"]
    return None


def linha_ml(conta: str, anuncio: dict, agora_iso: str) -> dict:
    sub_status = [s for s in (anuncio.get("sub_status") or []) if s]
    motivo = ", ".join(MOTIVOS_ML.get(s, s) for s in sub_status) or "Pausado pelo vendedor"
    return {
        "marketplace": "mercado_livre",
        "conta": conta,
        "anuncio_id": str(anuncio["id"]),
        "titulo": anuncio.get("title"),
        "sku": _sku_ml(anuncio),
        "preco": anuncio.get("price"),
        "estoque": anuncio.get("available_quantity"),
        "status": anuncio.get("status") or "paused",
        "motivo": motivo,
        "link": anuncio.get("permalink"),
        "pausado_desde": anuncio.get("last_updated"),
        "atualizado_em": agora_iso,
    }


def linha_shopee(loja: str, anuncio: dict, agora_iso: str) -> dict:
    precos = anuncio.get("price_info") or []
    preco = precos[0].get("current_price") if precos else None
    estoque = ((anuncio.get("stock_info_v2") or {}).get("summary_info") or {}).get("total_available_stock")
    update_time = anuncio.get("update_time")
    item_id = str(anuncio["item_id"])
    return {
        "marketplace": "shopee",
        "conta": loja,
        "anuncio_id": item_id,
        "titulo": anuncio.get("item_name"),
        "sku": anuncio.get("item_sku") or None,
        "preco": preco,
        "estoque": estoque,
        "status": anuncio.get("item_status") or "UNLIST",
        "motivo": "Deslistado",
        "link": None,  # link publico precisa do shop_id; o dashboard monta a partir do anuncio_id se preciso
        "pausado_desde": datetime.fromtimestamp(update_time, tz=timezone.utc).isoformat() if update_time else None,
        "atualizado_em": agora_iso,
    }


def _headers() -> tuple[str, dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return url, {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def substituir_snapshot(marketplace: str, conta: str, linhas: list[dict]) -> None:
    """Apaga o snapshot anterior da conta e grava o atual."""
    url, headers = _headers()
    resp = requests.delete(
        f"{url}/rest/v1/{TABELA}",
        params={"marketplace": f"eq.{marketplace}", "conta": f"eq.{conta}"},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    for i in range(0, len(linhas), 500):
        resp = requests.post(
            f"{url}/rest/v1/{TABELA}?on_conflict=marketplace,conta,anuncio_id",
            json=linhas[i : i + 500],
            headers={**headers, "Prefer": "resolution=merge-duplicates"},
            timeout=60,
        )
        resp.raise_for_status()


def main():
    load_dotenv()
    agora_iso = datetime.now(timezone.utc).isoformat()

    for conta in CONTAS_ML:
        try:
            anuncios = MLClient(conta).obter_anuncios_por_status("paused")
            substituir_snapshot("mercado_livre", conta, [linha_ml(conta, a, agora_iso) for a in anuncios])
            logger.info("[%s] %d anuncio(s) pausado(s)", conta, len(anuncios))
        except (MLApiError, requests.RequestException):
            logger.exception("[%s] Falha ao atualizar anuncios pausados", conta)
        except Exception:
            logger.exception("[%s] Erro inesperado atualizando anuncios pausados", conta)

    for loja in LOJAS_SHOPEE:
        try:
            anuncios = ShopeeClient(loja, ambiente="prod").obter_anuncios_por_status("UNLIST")
            substituir_snapshot("shopee", loja, [linha_shopee(loja, a, agora_iso) for a in anuncios])
            logger.info("[%s] %d anuncio(s) deslistado(s)", loja, len(anuncios))
        except (ShopeeApiError, requests.RequestException):
            logger.exception("[%s] Falha ao atualizar anuncios pausados", loja)
        except Exception:
            logger.exception("[%s] Erro inesperado atualizando anuncios pausados", loja)


if __name__ == "__main__":
    main()
