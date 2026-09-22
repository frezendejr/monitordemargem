"""Job standalone da checagem de estoque/Ads (grade furada, estoque zerado) -
separado do sync de pedido (monitor.py/monitor_cloud.py) porque e bem mais
lenta (chama a API do Meli item por item pra cada anuncio de Calcados/
Utilidade Domestica) e nao precisa da mesma frequencia do sync de margem.

Agendado 2x ao dia (ver estoque-ads.timer na VM), nao a cada ciclo do
monitor - antes rodava junto de monitor_cloud.py e deixava o ciclo inteiro
(que devia ser rapido, so sync de pedido) levando ~10-11 min.

Uso: python estoque_ads_job.py
"""

from __future__ import annotations

import logging
import sys

import yaml
from dotenv import load_dotenv

from alerts import enviar_email, enviar_whatsapp
from estoque_ads import verificar_alertas_estoque

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("estoque_ads_job")

CONFIG_PATH_PADRAO = "config.yaml"


def main():
    load_dotenv()
    with open(CONFIG_PATH_PADRAO, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    novos_alertas = verificar_alertas_estoque(config)
    for alerta in novos_alertas:
        mensagem = (
            f"⚠️ {alerta['tipo'].replace('_', ' ').upper()} - {alerta['canal']}\n"
            f"Anúncio: {alerta['anuncio_id']}\n{alerta['detalhe']}"
        )
        enviar_whatsapp(mensagem, config)
        enviar_email(f"Alerta de estoque/Ads - {alerta['anuncio_id']}", mensagem, config)
        logger.warning("Novo alerta de estoque: %s", mensagem)

    logger.info("Checagem de estoque/ads concluida: %d alerta(s) novo(s)", len(novos_alertas))


if __name__ == "__main__":
    main()
