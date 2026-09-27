"""Recalcula o % de Ads (TACOS) de cada loja Shopee 1x ao dia - gasto de
Ads dividido pelo faturamento bruto dos ultimos 29 dias (definicao pedida
pelo time: "normalmente definimos 8%", calculado com dado real em vez de
numero fixo no config.yaml). Grava em margin_monitor_shopee_ads_pct;
shopee_pedidos.py le de la a cada ciclo (com fallback pro config.yaml se
ainda nao tiver rodado nenhuma vez).

Roda separado do sync de pedido porque o endpoint de Ads da Shopee tem rate
limit bem mais apertado que o resto da API - nao pode ser chamado a cada
ciclo de poucos minutos (ver estoque_ads_job.py/sefaz-nfe pelo mesmo motivo
de "separar o que e lento/limitado do que precisa ser frequente").

Uso: python shopee_ads_pct_job.py
"""

from __future__ import annotations

import logging
import os
import sys

import requests
from dotenv import load_dotenv

from shopee_client import ShopeeApiError, ShopeeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("shopee_ads_pct_job")

LOJAS = ["shopee_1", "shopee_2"]


def _gravar(loja: str, ads_pct: float, gasto_total: float, faturamento_total: float) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_shopee_ads_pct?on_conflict=loja",
        json={
            "loja": loja,
            "ads_pct": ads_pct,
            "gasto_total": gasto_total,
            "faturamento_total": faturamento_total,
        },
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=15,
    )
    resp.raise_for_status()


def main():
    load_dotenv()
    for loja in LOJAS:
        try:
            cliente = ShopeeClient(loja, ambiente="prod")
            gasto_total, faturamento_total = cliente.obter_tacos_periodo()
            if faturamento_total <= 0:
                logger.warning("[%s] Faturamento total zerado nos ultimos 29 dias - pulando (sem dado suficiente)", loja)
                continue
            ads_pct = round(gasto_total / faturamento_total * 100, 2)
            _gravar(loja, ads_pct, gasto_total, faturamento_total)
            logger.info(
                "[%s] TACOS recalculado: %.2f%% (gasto R$%.2f / faturamento R$%.2f)",
                loja, ads_pct, gasto_total, faturamento_total,
            )
        except ShopeeApiError:
            logger.exception("[%s] Falha ao calcular TACOS - mantem o valor anterior", loja)
        except Exception:
            logger.exception("[%s] Erro inesperado calculando TACOS", loja)


if __name__ == "__main__":
    main()
