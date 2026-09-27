"""Recalcula 1x ao dia o snapshot de performance de Ads por campanha (ultimos
7 dias) pras 2 lojas Shopee e as 4 contas Mercado Livre - alimenta a aba
"Monitor de Ads" do dashboard (ver dashboard.py). Grava em
margin_monitor_ads_campanhas (upsert por marketplace+conta+campanha_id -
sempre reflete o snapshot MAIS RECENTE, nao mantem historico).

Meta do time (27/09/2026): TACOS (gasto de Ads / faturamento) acima de 10%
ou ROAS abaixo de 5 e sinal de alerta - a flag em si e calculada no
dashboard, aqui so grava o numero cru.

Uso: python ads_monitor_job.py
"""

from __future__ import annotations

import logging
import os
import sys

import requests
from dotenv import load_dotenv

from ml_client import MLApiError, MLClient
from shopee_client import ShopeeApiError, ShopeeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ads_monitor_job")

CONTAS_ML = ["meli_conta_1", "meli_conta_2", "meli_conta_3", "meli_conta_4"]
LOJAS_SHOPEE = ["shopee_1", "shopee_2"]
DIAS_PERIODO = 7


def _gravar(linhas: list[dict]) -> None:
    if not linhas:
        return
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_ads_campanhas?on_conflict=marketplace,conta,campanha_id",
        json=linhas,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=30,
    )
    resp.raise_for_status()


def processar_ml(conta: str) -> None:
    cliente = MLClient(conta)
    advertiser_id = cliente.obter_advertiser_id()
    if advertiser_id is None:
        logger.info("[%s] Sem Mercado Ads habilitado - pulando", conta)
        return

    campanhas = cliente.obter_campanhas_com_metricas(advertiser_id, dias=DIAS_PERIODO)
    linhas = []
    for c in campanhas:
        metricas = c.get("metrics") or {}
        gasto = float(metricas.get("cost") or 0)
        faturamento = float(metricas.get("total_amount") or 0)
        linhas.append(
            {
                "marketplace": "mercado_livre",
                "conta": conta,
                "campanha_id": str(c["id"]),
                "campanha_nome": c.get("name"),
                "dias_periodo": DIAS_PERIODO,
                "gasto": gasto,
                "faturamento": faturamento,
                "roas": float(metricas.get("roas") or 0),
                "tacos": float(metricas.get("acos") or 0),
            }
        )
    _gravar(linhas)
    logger.info("[%s] %d campanha(s) atualizada(s)", conta, len(linhas))


def processar_shopee(loja: str) -> None:
    cliente = ShopeeClient(loja, ambiente="prod")
    campanhas = cliente.obter_campanhas_com_metricas(dias=DIAS_PERIODO)
    linhas = [
        {
            "marketplace": "shopee",
            "conta": loja,
            "campanha_id": str(c["campaign_id"]),
            "campanha_nome": c.get("ad_name"),
            "dias_periodo": DIAS_PERIODO,
            "gasto": c["gasto"],
            "faturamento": c["faturamento"],
            "roas": c["roas"],
            "tacos": c["tacos"],
        }
        for c in campanhas
    ]
    _gravar(linhas)
    logger.info("[%s] %d campanha(s) atualizada(s)", loja, len(linhas))


def main():
    load_dotenv()

    for conta in CONTAS_ML:
        try:
            processar_ml(conta)
        except MLApiError:
            logger.exception("[%s] Falha ao atualizar campanhas de Ads", conta)
        except Exception:
            logger.exception("[%s] Erro inesperado atualizando campanhas de Ads", conta)

    for loja in LOJAS_SHOPEE:
        try:
            processar_shopee(loja)
        except ShopeeApiError:
            logger.exception("[%s] Falha ao atualizar campanhas de Ads", loja)
        except Exception:
            logger.exception("[%s] Erro inesperado atualizando campanhas de Ads", loja)


if __name__ == "__main__":
    main()
