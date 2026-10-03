"""Sync de pedidos DIRETO da API da Shopee - decisao do time (22/09/2026):
Shopee 1 e Shopee 2 NAO usam mais o Tiny como fonte de pedido. Pedidos,
itens e financeiro vem tudo daqui (diferente do Mercado Livre, que continua
com o Tiny como fonte - so enriquecido com receita exata, ver ml_client.py).

Cada loja Shopee tem seu PROPRIO checkpoint em margin_monitor_checkpoint,
chaveado pela loja ("shopee_1"/"shopee_2"), NAO pelo conta_tiny - senao
colidiria com o checkpoint do sync via Tiny da MESMA conta_tiny (ex.:
conta_amoshoes tambem sincroniza Meli Conta 2/3 via Tiny, com seu proprio
checkpoint). O conta_tiny gravado no pedido continua sendo o da empresa
dona da loja (so pra manter o agrupamento do dashboard) - a API do Tiny
nunca e chamada aqui.

get_order_list limita a janela a 15 dias por chamada (documentado) - se o
checkpoint estiver mais velho que isso, corta em 15 dias (proximo ciclo
continua de onde parou, igual qualquer sync incremental defasado).

Pedidos em status UNPAID/CANCELLED nao tem escrow (nada foi pago ainda) -
pula sem tentar (evita uma chamada fadada a falhar); demais status tentam
puxar o escrow e sao processados assim que disponivel (nao espera
COMPLETED - o objetivo e alertar rapido, mesma filosofia do resto do
projeto, ver margin_engine.py).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

import storage_supabase as storage
import supabase_writer
from alerts import enviar_email, enviar_whatsapp, montar_mensagem_alerta
from margin_engine import ItemPedido, calcular_margem
from shopee_client import ShopeeApiError, ShopeeClient

logger = logging.getLogger(__name__)

FUSO_BRASILIA = timezone(timedelta(hours=-3))
JANELA_MAXIMA_SEGUNDOS = 15 * 86400 - 3600  # 15 dias com 1h de folga de seguranca
STATUS_SEM_ESCROW = {"UNPAID", "CANCELLED", "INVOICE_PENDING"}


def _ja_gravado_via_tiny(loja: str, order_sn: str) -> bool:
    """Pedidos anteriores a virada pra API direta (27/09/2026) estao gravados com o
    numero do Tiny, e o order_sn da Shopee fica em numero_ecommerce. A Shopee reenvia
    pedido antigo sempre que ele muda de status - sem essa checagem ele entrava de novo
    como duplicata (mesma venda contada 2x, achado em 03/10/2026)."""
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_itens",
        params={"canal": f"eq.{loja}", "numero_ecommerce": f"eq.{order_sn}", "select": "numero_pedido", "limit": "1"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def _sku_do_item(item: dict) -> str:
    return str(item.get("model_sku") or item.get("item_sku") or item.get("item_id"))


def _montar_itens(detalhe_pedido: dict, custos: dict) -> list[ItemPedido]:
    itens = []
    for item in detalhe_pedido.get("item_list", []):
        sku = _sku_do_item(item)
        qtd = float(item.get("model_quantity_purchased") or 0)
        valor_unit = float(item.get("model_discounted_price") or 0)
        itens.append(ItemPedido(sku=sku, quantidade=qtd, valor_unitario=valor_unit, custo_unitario=custos.get(sku)))
    return itens


def processar_loja_shopee(loja: str, conta_tiny: str, canal_config: dict, config: dict, custos: dict) -> int:
    cliente = ShopeeClient(loja, ambiente="prod")
    ads_pct = canal_config.get("ads_pct", 0.0)  # fixo (decisao do time, 27/09/2026) - ver "Monitor de Ads" pro TACOS real

    checkpoint = storage.obter_checkpoint(loja)
    agora = datetime.now(timezone.utc)
    desde = datetime.fromisoformat(checkpoint) if checkpoint else agora - timedelta(hours=24)

    time_from = int(desde.timestamp())
    time_to = int(agora.timestamp())
    if time_to - time_from > JANELA_MAXIMA_SEGUNDOS:
        time_to = time_from + JANELA_MAXIMA_SEGUNDOS
        proxima_marca = datetime.fromtimestamp(time_to, tz=timezone.utc)
    else:
        proxima_marca = agora

    order_sns: list[str] = []
    order_status: dict[str, str] = {}
    cursor = ""
    try:
        while True:
            resp = cliente.obter_lista_pedidos(time_from, time_to, cursor=cursor)
            body = resp["response"]
            for o in body.get("order_list", []):
                order_sns.append(o["order_sn"])
                order_status[o["order_sn"]] = o.get("order_status", "")
            if not body.get("more"):
                break
            cursor = body.get("next_cursor", "")
            if not cursor:
                break
    except Exception:
        logger.exception("[%s] Falha ao listar pedidos da Shopee - pulando esse ciclo", loja)
        return 0

    logger.info("[%s] %d pedido(s) atualizado(s) desde %s", loja, len(order_sns), desde.isoformat())

    processados = 0
    houve_falha_de_api = False

    for order_sn in order_sns:
        if storage.ja_processado(conta_tiny, order_sn):
            continue
        if order_status.get(order_sn) in STATUS_SEM_ESCROW:
            continue
        if _ja_gravado_via_tiny(loja, order_sn):
            continue

        try:
            detalhe = cliente.obter_detalhe_pedido(order_sn)["response"]["order_list"][0]
            financeiro = cliente.obter_detalhe_financeiro_pedido(order_sn)
        except ShopeeApiError:
            logger.exception("[%s] Falha ao obter detalhe/escrow do pedido %s - tenta de novo no proximo ciclo", loja, order_sn)
            continue
        except Exception:
            logger.exception("[%s] Erro inesperado no pedido %s - pulando", loja, order_sn)
            continue

        itens = _montar_itens(detalhe, custos)
        # DD/MM/AAAA em horario de Brasilia - mesmo formato que o Tiny/ML gravam e que
        # o dashboard exige (pd.to_datetime format="%d/%m/%Y"); ISO ou UTC fazia a venda
        # sumir dos filtros / cair no dia errado (bug real, achado em 03/10/2026).
        data_pedido = datetime.fromtimestamp(detalhe["create_time"], tz=FUSO_BRASILIA).strftime("%d/%m/%Y")

        # Mesma tecnica do Mercado Livre (ver monitor_cloud.py): comissao/frete
        # exatos vem por fora, calcular_margem roda com essas duas zeradas pra
        # nao duplicar. Ads da Shopee e cobrado por fora do escrow (so um
        # "top up" residual aparece la) - continua estimado por % (ads_pct)
        # sobre o VALOR DE VENDA bruto, igual ML.
        canal_config_exato = dict(canal_config, comissao_pct=0.0, frete_pct=0.0)
        resultado = calcular_margem(
            numero_pedido=order_sn,
            canal=loja,
            itens=itens,
            receita=financeiro.receita_liquida,
            canal_config=canal_config_exato,
            data_pedido=data_pedido,
        )
        resultado.comissao = financeiro.comissao_real
        resultado.frete = financeiro.frete_real

        ads_correto = round(financeiro.valor_venda * ads_pct / 100, 2)
        resultado.margem_contribuicao = round(resultado.margem_contribuicao - (ads_correto - resultado.ads), 2)
        resultado.margem_pct = round(resultado.margem_contribuicao / resultado.receita * 100, 2) if resultado.receita else 0.0
        resultado.ads = ads_correto

        threshold = config.get("alertas", {}).get("margem_negativa_threshold", 0)
        deve_alertar = resultado.margem_contribuicao < threshold
        if deve_alertar:
            mensagem = montar_mensagem_alerta(resultado)
            enviar_whatsapp(mensagem, config)
            enviar_email(f"Margem negativa - pedido {order_sn} ({loja})", mensagem, config)
            logger.warning("[%s] ALERTA margem negativa: pedido %s", loja, order_sn)

        try:
            supabase_writer.enviar_pedido(conta_tiny, resultado, alertado=deve_alertar, valor_venda=financeiro.valor_venda)
            supabase_writer.enviar_itens(conta_tiny, resultado)
        except supabase_writer.SupabaseError:
            logger.exception("[%s] Falha ao gravar pedido %s no Supabase - sera tentado de novo", loja, order_sn)
            houve_falha_de_api = True
            continue

        processados += 1

    if houve_falha_de_api:
        logger.warning("[%s] Checkpoint NAO avancado por causa de falha nesse ciclo", loja)
    else:
        storage.salvar_checkpoint(loja, proxima_marca.isoformat())

    return processados
