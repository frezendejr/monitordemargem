"""Sync de pedidos DIRETO da API do Mercado Livre - decisao do time
(03/10/2026): as 4 contas Meli deixam de usar o Tiny como fonte de pedido
(o conector Tiny<->ML perdeu um pedido pago real - 2000018759086068 - e nao
tem como saber o que mais fica de fora). Mesmo desenho do shopee_pedidos.py.

- Checkpoint por conta, em margin_monitor_checkpoint, chaveado pelo canal
  ("meli_conta_1"...), NAO pelo conta_tiny (senao colidiria com o checkpoint
  do sync via Tiny da mesma conta, que ainda cuida dos outros canais).
- O conta_tiny gravado e o da empresa dona da conta (so agrupamento do
  dashboard) - o Tiny nunca e chamado aqui.
- numero_pedido = id do pedido no ML; ele tambem vai em numero_ecommerce dos
  itens (e como o dashboard cruza com o marketplace).
- Pedidos anteriores a virada estao gravados com o numero do Tiny (id do
  pedido OU do pack em numero_ecommerce) - pula quem ja existe assim, senao
  o mesmo pedido entraria 2x. Pedido pago que o Tiny nao importou (o motivo
  da troca) entra normalmente.
- Receita/comissao/frete/anuncios exatos: ml_client.obter_detalhe_financeiro_pedido
  (calibrado contra extrato real). Pedido de pack: cada order tem o PROPRIO
  pagamento (confirmado em packs reais), entao processa order a order.
- Sem checkpoint (primeira execucao) busca os ultimos 4 dias - cobre o buraco
  recente do Tiny; a trava acima evita duplicar o resto.
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
from ml_client import MLApiError, MLClient

logger = logging.getLogger(__name__)

FUSO_BRASILIA = timezone(timedelta(hours=-3))
BACKFILL_INICIAL = timedelta(days=4)
# Pedido CRIADO antes disso e historico do Tiny: o ML reenvia pedido antigo toda
# vez que ele muda de status (entrega, devolucao...), e as linhas antigas do Tiny
# nao tem o numero do ML gravado, entao a checagem por numero_ecommerce nao pega
# - sem este corte ~25 pedidos antigos entraram de novo como duplicata (e
# dispararam alerta de WhatsApp falso), achado no primeiro teste real em 03/10/2026.
CORTE_CRIACAO = datetime(2026, 9, 29, 0, 0, tzinfo=FUSO_BRASILIA)


def _ja_gravado_via_tiny(canal: str, order_id: str, pack_id: str | None) -> bool:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    ids = [order_id] + ([pack_id] if pack_id else [])
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_itens",
        params={"canal": f"eq.{canal}", "numero_ecommerce": f"in.({','.join(ids)})", "select": "numero_pedido", "limit": "1"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def _montar_itens(pedido_ml: dict, custos: dict) -> list[ItemPedido]:
    itens = []
    for oi in pedido_ml.get("order_items", []):
        info = oi.get("item") or {}
        sku = str(info.get("seller_sku") or info.get("seller_custom_field") or info.get("id"))
        itens.append(
            ItemPedido(
                sku=sku,
                quantidade=float(oi.get("quantity") or 0),
                valor_unitario=float(oi.get("unit_price") or 0),
                custo_unitario=custos.get(sku),
            )
        )
    return itens


def processar_conta_ml(canal: str, conta_tiny: str, canal_config: dict, config: dict, custos: dict) -> int:
    cliente = MLClient(canal)
    agora = datetime.now(timezone.utc)
    checkpoint = storage.obter_checkpoint(canal)
    desde = datetime.fromisoformat(checkpoint) if checkpoint else agora - BACKFILL_INICIAL

    try:
        pedidos = cliente.buscar_pedidos_pagos_atualizados(desde, agora)
    except Exception:
        logger.exception("[%s] Falha ao listar pedidos do Mercado Livre - pulando esse ciclo", canal)
        return 0

    logger.info("[%s] %d pedido(s) pago(s) atualizado(s) desde %s", canal, len(pedidos), desde.isoformat())

    ads_pct = canal_config.get("ads_pct", 0.0)
    processados = 0
    houve_falha = False

    for pedido_ml in pedidos:
        order_id = str(pedido_ml["id"])
        pack_id = str(pedido_ml["pack_id"]) if pedido_ml.get("pack_id") else None

        criado_em = datetime.fromisoformat(pedido_ml["date_created"])
        if criado_em < CORTE_CRIACAO:
            continue
        if storage.ja_processado(conta_tiny, order_id):
            continue
        if _ja_gravado_via_tiny(canal, order_id, pack_id):
            continue

        try:
            detalhe = cliente.obter_detalhe_financeiro_pedido(order_id)
        except MLApiError:
            logger.exception("[%s] Falha ao obter financeiro do pedido %s - tenta de novo no proximo ciclo", canal, order_id)
            houve_falha = True
            continue
        except Exception:
            logger.exception("[%s] Erro inesperado no pedido %s - pulando", canal, order_id)
            continue

        itens = _montar_itens(pedido_ml, custos)
        data_pedido = criado_em.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y")

        # Mesma tecnica do caminho Tiny+ML (monitor_cloud.py): receita = liquido real
        # (net_received_amount), comissao/frete exatos entram so como informacao
        # (ja estao descontados da receita) e o Ads (cobrado por fora) e % sobre o
        # valor de venda bruto.
        canal_config_exato = dict(canal_config, comissao_pct=0.0, frete_pct=0.0, faixas_frete=None)
        resultado = calcular_margem(
            numero_pedido=order_id,
            canal=canal,
            itens=itens,
            receita=detalhe.receita_liquida,
            canal_config=canal_config_exato,
            data_pedido=data_pedido,
        )
        resultado.comissao = detalhe.comissao_real
        resultado.frete = detalhe.frete_real

        ads_correto = round(detalhe.valor_venda * ads_pct / 100, 2)
        resultado.margem_contribuicao = round(resultado.margem_contribuicao - (ads_correto - resultado.ads), 2)
        resultado.margem_pct = (
            round(resultado.margem_contribuicao / resultado.receita * 100, 2) if resultado.receita else 0.0
        )
        resultado.ads = ads_correto

        threshold = config.get("alertas", {}).get("margem_negativa_threshold", 0)
        deve_alertar = resultado.margem_contribuicao < threshold
        if deve_alertar:
            mensagem = montar_mensagem_alerta(resultado)
            enviar_whatsapp(mensagem, config)
            enviar_email(f"Margem negativa - pedido {order_id} ({canal})", mensagem, config)
            logger.warning("[%s] ALERTA margem negativa: pedido %s", canal, order_id)

        try:
            supabase_writer.enviar_pedido(conta_tiny, resultado, alertado=deve_alertar, valor_venda=detalhe.valor_venda)
            supabase_writer.enviar_itens(
                conta_tiny, resultado, anuncios=detalhe.anuncios, numero_ecommerce=order_id
            )
        except supabase_writer.SupabaseError:
            logger.exception("[%s] Falha ao gravar pedido %s no Supabase - sera tentado de novo", canal, order_id)
            houve_falha = True
            continue

        processados += 1

    if houve_falha:
        logger.warning("[%s] Checkpoint NAO avancado por causa de falha nesse ciclo", canal)
    else:
        storage.salvar_checkpoint(canal, agora.isoformat())

    return processados
