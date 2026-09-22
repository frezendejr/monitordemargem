"""Alertas de estoque em anuncios do Mercado Livre - a pedido do time
(2026-09-21):

1. "Grade furada com ADS ativo" (categoria Calcados): um anuncio que tem
   Product Ads rodando (dinheiro sendo gasto em publicidade) mas a MAIORIA
   dos tamanhos daquela cor esta sem estoque - o anuncio continua atraindo
   clique/venda que depois "estoura" por falta de tamanho, desperdicando
   verba de Ads e virando detrator de margem. Furada = menos da metade das
   variacoes (tamanhos) daquela cor com estoque > 0.

2. "Estoque zerado" (categoria Utilidade Domestica): anuncio sem variacao
   (produto simples) com available_quantity = 0 - precisa pausar antes que
   venda algo que nao tem mais.

So ALERTA (WhatsApp/e-mail + pisca no dashboard) por enquanto - pausar o
anuncio automaticamente fica pra depois de validar que a deteccao esta
acertando (ver pausar_anuncio(), ja pronta mas NUNCA chamada
automaticamente ainda).

Roda 1x por ciclo do monitor (nao precisa ser tao frequente quanto o sync
de pedido) - so pros canais Meli, ja que a integracao Shopee ainda esta em
andamento (ver shopee_client.py).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from categoria_produto import _codigo_pai, carregar_categorias
from ml_client import MLApiError, MLClient

logger = logging.getLogger(__name__)

CATEGORIA_GRADE = "Calçados"
CATEGORIA_ESTOQUE_ZERADO = "Utilidade Doméstica"


def analisar_grade_por_cor(item: dict) -> list[dict]:
    """Agrupa as variacoes do anuncio por Cor (uma "grade" de tamanhos e
    por cor, nao o anuncio inteiro - anuncio com 2 cores tem 2 grades
    distintas). Retorna 1 dict por cor: {cor, total, com_estoque, furada}."""
    por_cor: dict[str, list[int]] = {}
    for var in item.get("variations") or []:
        attrs = {a.get("name"): a.get("value_name") for a in var.get("attribute_combinations", [])}
        cor = attrs.get("Cor") or "Única"
        por_cor.setdefault(cor, []).append(int(var.get("available_quantity") or 0))

    grades = []
    for cor, quantidades in por_cor.items():
        total = len(quantidades)
        com_estoque = sum(1 for q in quantidades if q > 0)
        grades.append(
            {
                "cor": cor,
                "total_tamanhos": total,
                "tamanhos_com_estoque": com_estoque,
                "furada": com_estoque < total / 2 if total else False,
            }
        )
    return grades


def _itens_por_categoria(categoria_alvo: str, categorias: dict[str, str]) -> list[dict]:
    """{conta_tiny, canal, anuncio_id, codigo_pai} - 1 linha por anuncio_id
    distinto que ja vendeu alguma vez e cujo codigo_pai esta classificado
    como `categoria_alvo`. So canais meli_* (Ads e essa API de estoque so
    existem no Mercado Livre por enquanto)."""
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    itens: list[dict] = []
    inicio = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/margin_monitor_itens",
            headers={**headers, "Range": f"{inicio}-{inicio + 999}"},
            params={"select": "conta_tiny,canal,anuncio_id,sku", "anuncio_id": "not.is.null"},
            timeout=30,
        )
        resp.raise_for_status()
        pagina = resp.json()
        itens.extend(pagina)
        if len(pagina) < 1000:
            break
        inicio += 1000

    vistos: set[str] = set()
    resultado = []
    for item in itens:
        if not str(item["canal"]).startswith("meli_conta"):
            continue
        codigo_pai = _codigo_pai(item["sku"])
        if categorias.get(codigo_pai) != categoria_alvo:
            continue
        if item["anuncio_id"] in vistos:
            continue
        vistos.add(item["anuncio_id"])
        resultado.append(
            {
                "conta_tiny": item["conta_tiny"],
                "canal": item["canal"],
                "anuncio_id": item["anuncio_id"],
                "codigo_pai": codigo_pai,
            }
        )
    return resultado


def _alerta_ja_ativo(anuncio_id: str, tipo: str) -> bool:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_alertas_estoque",
        headers=headers,
        params={"anuncio_id": f"eq.{anuncio_id}", "tipo": f"eq.{tipo}", "ativo": "eq.true", "select": "id"},
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def _registrar_alerta(conta_tiny: str, canal: str, anuncio_id: str, tipo: str, detalhe: str) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_alertas_estoque?on_conflict=anuncio_id,tipo",
        json={
            "conta_tiny": conta_tiny,
            "canal": canal,
            "anuncio_id": anuncio_id,
            "tipo": tipo,
            "detalhe": detalhe,
            "ativo": True,
            "resolvido_em": None,
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


def _resolver_alerta(anuncio_id: str, tipo: str) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.patch(
        f"{url}/rest/v1/margin_monitor_alertas_estoque",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        params={"anuncio_id": f"eq.{anuncio_id}", "tipo": f"eq.{tipo}", "ativo": "eq.true"},
        json={"ativo": False, "resolvido_em": datetime.now(timezone.utc).isoformat()},
        timeout=15,
    )
    resp.raise_for_status()


def pausar_anuncio(cliente: MLClient, item_id: str) -> None:
    """Pausa o anuncio (status="paused") - PRONTA MAS NUNCA CHAMADA
    AUTOMATICAMENTE ainda. So usar depois de validar com o time que a
    deteccao de grade furada/estoque zerado esta acertando de verdade -
    pausar um anuncio ativo por engano tem custo real (perde posicao/
    historico de venda)."""
    access_token = cliente._access_token_valido()
    resp = requests.put(
        f"https://api.mercadolibre.com/items/{item_id}",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"status": "paused"},
        timeout=15,
    )
    if not resp.ok:
        raise MLApiError(f"Falha ao pausar anuncio {item_id}: {resp.status_code} {resp.text}")


def verificar_alertas_estoque(config: dict) -> list[dict]:
    """Roda a checagem completa (grade furada + ADS ativo, estoque zerado)
    e retorna a lista de alertas NOVOS disparados nesse ciclo (pra
    montar_mensagem_alerta_estoque + enviar_whatsapp/enviar_email). Falha
    de rede/API aqui NUNCA pode derrubar o ciclo do monitor - mesma regra
    de recalcular_custo_pendente/recalcular_categoria_pendente."""
    try:
        categorias = carregar_categorias()
    except Exception:
        logger.exception("Falha ao carregar categorias - pulando checagem de estoque/ads nesse ciclo")
        return []

    ml_clientes: dict[str, MLClient] = {}
    novos_alertas: list[dict] = []

    # --- 1. Grade furada com ADS ativo (Calcados) ---------------------
    try:
        itens_calcados = _itens_por_categoria(CATEGORIA_GRADE, categorias)
        por_canal: dict[str, list[dict]] = {}
        for item in itens_calcados:
            por_canal.setdefault(item["canal"], []).append(item)

        for canal, itens_canal in por_canal.items():
            if canal not in ml_clientes:
                ml_clientes[canal] = MLClient(canal)
            cliente = ml_clientes[canal]
            try:
                advertiser_id = cliente.obter_advertiser_id()
            except MLApiError:
                logger.exception("[%s] Falha ao obter advertiser_id - pulando grade furada desse canal", canal)
                continue
            if advertiser_id is None:
                continue

            ids_canal = [i["anuncio_id"] for i in itens_canal]
            try:
                itens_com_ads = cliente.obter_itens_com_ads(ids_canal, advertiser_id)
            except MLApiError:
                logger.exception("[%s] Falha ao checar Ads em lote - pulando grade furada desse canal", canal)
                continue

            for item in itens_canal:
                anuncio_id = item["anuncio_id"]
                if anuncio_id not in itens_com_ads:
                    continue  # sem Ads ativo - fora do escopo desse alerta
                try:
                    detalhe_item = cliente.obter_item(anuncio_id)
                except MLApiError:
                    logger.exception("[%s] Falha ao obter detalhe do item %s", canal, anuncio_id)
                    continue

                grades = analisar_grade_por_cor(detalhe_item)
                furadas = [g for g in grades if g["furada"]]
                if furadas:
                    detalhe_txt = "; ".join(
                        f"{g['cor']}: {g['tamanhos_com_estoque']}/{g['total_tamanhos']} tamanhos com estoque"
                        for g in furadas
                    )
                    if not _alerta_ja_ativo(anuncio_id, "grade_furada"):
                        _registrar_alerta(item["conta_tiny"], canal, anuncio_id, "grade_furada", detalhe_txt)
                        novos_alertas.append(
                            {"tipo": "grade_furada", "canal": canal, "anuncio_id": anuncio_id, "detalhe": detalhe_txt}
                        )
                    else:
                        _registrar_alerta(item["conta_tiny"], canal, anuncio_id, "grade_furada", detalhe_txt)
                else:
                    if _alerta_ja_ativo(anuncio_id, "grade_furada"):
                        _resolver_alerta(anuncio_id, "grade_furada")
    except Exception:
        logger.exception("Falha checando grade furada - seguindo pro proximo bloco")

    # --- 2. Estoque zerado (Utilidade Domestica) -----------------------
    try:
        itens_utilidade = _itens_por_categoria(CATEGORIA_ESTOQUE_ZERADO, categorias)
        for item in itens_utilidade:
            canal = item["canal"]
            if canal not in ml_clientes:
                ml_clientes[canal] = MLClient(canal)
            try:
                detalhe_item = ml_clientes[canal].obter_item(item["anuncio_id"])
            except MLApiError:
                logger.exception("[%s] Falha ao obter detalhe do item %s", canal, item["anuncio_id"])
                continue

            zerado = (detalhe_item.get("available_quantity") or 0) <= 0
            anuncio_id = item["anuncio_id"]
            if zerado:
                detalhe_txt = "Estoque zerado - recomendado pausar o anúncio"
                if not _alerta_ja_ativo(anuncio_id, "estoque_zerado"):
                    _registrar_alerta(item["conta_tiny"], canal, anuncio_id, "estoque_zerado", detalhe_txt)
                    novos_alertas.append(
                        {"tipo": "estoque_zerado", "canal": canal, "anuncio_id": anuncio_id, "detalhe": detalhe_txt}
                    )
            else:
                if _alerta_ja_ativo(anuncio_id, "estoque_zerado"):
                    _resolver_alerta(anuncio_id, "estoque_zerado")
    except Exception:
        logger.exception("Falha checando estoque zerado")

    return novos_alertas
