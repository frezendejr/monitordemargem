"""Cliente da API da Shopee (Open Platform v2, OAuth2 + assinatura HMAC-SHA256).

Complementa o tiny_client.py: pros canais Shopee (shopee_1, shopee_2 - ver
config.yaml) onde a autorizacao abaixo for feita, o monitor podera consultar
a API da Shopee pra pegar comissao/frete/Ads REAIS do pedido, em vez de
estimar por % fixo (mesmo padrao ja usado pro Mercado Livre - ver
ml_client.py, DetalheFinanceiroPedido/obter_detalhe_financeiro_pedido).

IMPORTANTE - NAO CALIBRADO AINDA: os nomes exatos dos campos financeiros
(comissao, frete, Ads) dentro do retorno de get_escrow_detail ainda NAO
foram confirmados contra um pedido real (so contra documentacao publica de
terceiros, que diverge em detalhe). obter_detalhe_financeiro_pedido()
propositalmente levanta NotImplementedError ate isso ser calibrado - mesma
disciplina usada em todo o projeto (nunca confiar em nome de campo sem
confirmar contra resposta real da API, ver README/ml_client.py).

Fluxo de autorizacao (manual, uma vez por loja Shopee):
    python shopee_client.py --loja shopee_1 --auth-url
    # abrir a URL impressa (valida so 5 minutos!), logar como ADMIN da loja,
    # autorizar o app - a Shopee redireciona pra
    #   SHOPEE_REDIRECT_URI?code=XXXX&shop_id=NNNNN
    # (mesmo que a pagina de 404) - copiar os dois valores da URL
    python shopee_client.py --loja shopee_1 --exchange-code XXXX --shop-id NNNNN
    # salva o access_token/refresh_token em shopee_tokens_shopee_1.json

Depois disso, os metodos de leitura renovam o access_token sozinhos quando
necessario (access_token dura 4h, refresh_token dura 30 dias - prazos
documentados pela Shopee).

Comecar pelo ambiente de teste (--ambiente test, o padrao) contra a
"Test Account-Sandbox" do Shopee Open Platform Console antes de autorizar
lojas de producao de verdade.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class DetalheFinanceiroPedidoShopee:
    receita_liquida: float
    valor_venda: float
    comissao_real: float
    frete_real: float
    ads_real: float | None = None


BASE_URL_PROD = "https://partner.shopeemobile.com"
BASE_URL_TEST = "https://openplatform.sandbox.test-stable.shopee.sg"
# Confirmado direto no "API Test Tool" do Shopee Open Platform Console
# (2026-09-21) - documentacao publica de terceiros indicava
# "partner.test-stable.shopeemobile.com", que dava "Wrong sign" (era o
# dominio errado, nao um bug de assinatura).


class ShopeeApiError(RuntimeError):
    """Erro retornado pela API da Shopee (resposta com campo 'error' preenchido,
    ou HTTP nao-2xx)."""


def _base_url(ambiente: str) -> str:
    return BASE_URL_TEST if ambiente == "test" else BASE_URL_PROD


def _assinar(partner_key: str, base_string: str) -> str:
    return hmac.new(partner_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha256).hexdigest()


def montar_url_autorizacao(partner_id: str, partner_key: str, redirect_uri: str, ambiente: str = "test") -> str:
    """URL que o ADMIN da loja Shopee abre pra autorizar o app - a Shopee
    redireciona de volta pra `redirect_uri` com ?code=...&shop_id=...
    (copiar os 2 valores, mesmo que a pagina de 404). Link valido por SO
    5 MINUTOS - gerar de novo se demorar."""
    path = "/api/v2/shop/auth_partner"
    timestamp = int(time.time())
    sign = _assinar(partner_key, f"{partner_id}{path}{timestamp}")
    return (
        f"{_base_url(ambiente)}{path}?partner_id={partner_id}&timestamp={timestamp}"
        f"&sign={sign}&redirect={redirect_uri}"
    )


def _tokens_path(loja: str) -> Path:
    return Path(f"shopee_tokens_{loja}.json")


def _modo_nuvem() -> bool:
    """Em ambiente sem disco persistente (GitHub Actions), os tokens tem
    que ir pro Supabase - senao o refresh_token se perde entre execucoes.
    Ativado via MARGIN_MONITOR_CLOUD=1 (mesma variavel do ml_client.py)."""
    return os.environ.get("MARGIN_MONITOR_CLOUD") == "1"


def _salvar_tokens(loja: str, dados: dict) -> None:
    dados = dict(dados)
    dados["obtido_em"] = time.time()
    if _modo_nuvem():
        _salvar_tokens_supabase(loja, dados)
    else:
        _tokens_path(loja).write_text(json.dumps(dados, indent=2), encoding="utf-8")


def _carregar_tokens(loja: str) -> dict | None:
    if _modo_nuvem():
        return _carregar_tokens_supabase(loja)
    path = _tokens_path(loja)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _salvar_tokens_supabase(loja: str, dados: dict) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    payload = {
        "loja": loja,
        "shop_id": str(dados["shop_id"]),
        "access_token": dados["access_token"],
        "refresh_token": dados["refresh_token"],
        "expire_in": dados.get("expire_in"),
        "obtido_em": dados["obtido_em"],
    }
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_shopee_tokens?on_conflict=loja",
        json=payload,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=15,
    )
    resp.raise_for_status()


def _carregar_tokens_supabase(loja: str) -> dict | None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_shopee_tokens",
        params={"loja": f"eq.{loja}", "select": "*"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    linhas = resp.json()
    return linhas[0] if linhas else None


def trocar_code_por_token(
    loja: str, partner_id: str, partner_key: str, code: str, shop_id: str, ambiente: str = "test"
) -> dict:
    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())
    sign = _assinar(partner_key, f"{partner_id}{path}{timestamp}")
    resp = requests.post(
        f"{_base_url(ambiente)}{path}",
        params={"partner_id": partner_id, "timestamp": timestamp, "sign": sign},
        json={"code": code, "shop_id": int(shop_id), "partner_id": int(partner_id)},
        timeout=30,
    )
    if not resp.ok:
        raise ShopeeApiError(f"Falha ao trocar code por token: {resp.status_code} {resp.text}")
    dados = resp.json()
    if dados.get("error"):
        raise ShopeeApiError(f"Falha ao trocar code por token: {dados}")
    dados["shop_id"] = shop_id
    _salvar_tokens(loja, dados)
    return dados


def _renovar_com_refresh_token(
    loja: str, partner_id: str, partner_key: str, refresh_token: str, shop_id: str, ambiente: str = "test"
) -> dict:
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    sign = _assinar(partner_key, f"{partner_id}{path}{timestamp}")
    resp = requests.post(
        f"{_base_url(ambiente)}{path}",
        params={"partner_id": partner_id, "timestamp": timestamp, "sign": sign},
        json={"refresh_token": refresh_token, "shop_id": int(shop_id), "partner_id": int(partner_id)},
        timeout=30,
    )
    if not resp.ok:
        raise ShopeeApiError(
            f"Falha ao renovar token da loja '{loja}' (refresh_token pode ter expirado - "
            f"repetir --auth-url/--exchange-code pra essa loja): {resp.status_code} {resp.text}"
        )
    dados = resp.json()
    if dados.get("error"):
        raise ShopeeApiError(f"Falha ao renovar token da loja '{loja}': {dados}")
    dados["shop_id"] = shop_id
    _salvar_tokens(loja, dados)
    return dados


def _env_partner_id() -> str:
    return os.environ["SHOPEE_PARTNER_ID"]


def _env_partner_key() -> str:
    return os.environ["SHOPEE_PARTNER_KEY"]


class ShopeeClient:
    def __init__(
        self,
        loja: str,
        ambiente: str = "test",
        partner_id: str | None = None,
        partner_key: str | None = None,
    ):
        """`loja` e a chave do canal em config.yaml (ex.: "shopee_1") - define
        o arquivo/linha de tokens. partner_id/partner_key sao compartilhados
        (1 app Shopee Open Platform pra todas as lojas), vem do .env por
        padrao (SHOPEE_PARTNER_ID/SHOPEE_PARTNER_KEY)."""
        self.loja = loja
        self.ambiente = ambiente
        self.partner_id = partner_id or _env_partner_id()
        self.partner_key = partner_key or _env_partner_key()

    def _tokens_validos(self) -> dict:
        tokens = _carregar_tokens(self.loja)
        if not tokens:
            raise ShopeeApiError(
                f"Nenhum token salvo pra loja '{self.loja}' - rodar --auth-url e --exchange-code primeiro"
            )
        # access_token dura 4h (documentado pela Shopee) - renova com 5min de folga.
        expira_em = tokens["obtido_em"] + tokens.get("expire_in", 4 * 3600) - 300
        if time.time() >= expira_em:
            logger.info("[%s] Access token expirado/perto de expirar - renovando", self.loja)
            tokens = _renovar_com_refresh_token(
                self.loja, self.partner_id, self.partner_key, tokens["refresh_token"], tokens["shop_id"], self.ambiente
            )
        return tokens

    def _get(self, path: str, params: dict | None = None) -> dict:
        tokens = self._tokens_validos()
        timestamp = int(time.time())
        base_string = f"{self.partner_id}{path}{timestamp}{tokens['access_token']}{tokens['shop_id']}"
        sign = _assinar(self.partner_key, base_string)
        query = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign,
            "access_token": tokens["access_token"],
            "shop_id": tokens["shop_id"],
            **(params or {}),
        }
        resp = requests.get(f"{_base_url(self.ambiente)}{path}", params=query, timeout=30)
        if not resp.ok:
            raise ShopeeApiError(f"Falha em {path}: {resp.status_code} {resp.text}")
        dados = resp.json()
        if dados.get("error"):
            raise ShopeeApiError(f"Erro da API Shopee em {path}: {dados}")
        return dados

    def obter_lista_pedidos(
        self,
        time_from: int,
        time_to: int,
        cursor: str = "",
        time_range_field: str = "update_time",
        page_size: int = 100,
    ) -> dict:
        """/api/v2/order/get_order_list - lista resumida de pedidos (so
        order_sn + status) numa janela de tempo. Shopee limita a janela a
        no maximo 15 dias por chamada (documentado) - pra janelas maiores,
        chamar varias vezes deslizando time_from/time_to. Pagina via
        `cursor` (resposta traz mais_pagina/next_cursor - nome de campo
        exato AINDA NAO confirmado, ver --debug-lista).

        `time_range_field`: "create_time" (pra backfill/historico) ou
        "update_time" (pra sync incremental - equivalente ao
        buscar_pedidos_atualizados_desde do tiny_client.py)."""
        return self._get(
            "/api/v2/order/get_order_list",
            {
                "time_range_field": time_range_field,
                "time_from": time_from,
                "time_to": time_to,
                "page_size": page_size,
                "cursor": cursor,
                "response_optional_fields": "order_status",
            },
        )

    def obter_detalhe_pedido(self, order_sn: str) -> dict:
        """/api/v2/order/get_order_detail - dados gerais do pedido (itens,
        valores, status). NAO tem o detalhamento de taxas/comissao - isso
        so vem em obter_escrow_detalhe."""
        return self._get(
            "/api/v2/order/get_order_detail",
            {
                "order_sn_list": order_sn,
                "response_optional_fields": "order_status,total_amount,item_list,actual_shipping_fee",
            },
        )

    def obter_escrow_detalhe(self, order_sn: str) -> dict:
        """/api/v2/payment/get_escrow_detail - onde ficam comissao/taxas
        reais cobradas pela Shopee nesse pedido (campo order_income).
        NOMES DE CAMPO AINDA NAO CONFIRMADOS contra resposta real - usar
        --debug-escrow contra um pedido de verdade antes de usar em
        obter_detalhe_financeiro_pedido."""
        return self._get("/api/v2/payment/get_escrow_detail", {"order_sn": order_sn})

    def obter_detalhe_financeiro_pedido(self, order_sn: str) -> DetalheFinanceiroPedidoShopee:
        """AINDA NAO CALIBRADO - ver aviso no topo do arquivo. Rodar:
            python shopee_client.py --loja <loja> --debug-escrow <order_sn>
        contra um pedido real (sandbox ou producao) primeiro, conferir os
        nomes de campo de comissao/frete/ads em order_income, e so entao
        preencher a extracao aqui (mesmo processo ja feito com o
        Mercado Livre - ver ml_client.py.obter_detalhe_financeiro_pedido)."""
        raise NotImplementedError(
            "Ainda nao calibrado contra pedido real - rode "
            f"'python shopee_client.py --loja {self.loja} --debug-escrow <order_sn>' primeiro."
        )


def _main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Depuracao do cliente Shopee")
    parser.add_argument("--loja", required=True, metavar="CHAVE", help="Chave do canal Shopee, ex.: shopee_1")
    parser.add_argument("--ambiente", default="test", choices=["test", "prod"], help="Sandbox (test, padrao) ou producao")
    parser.add_argument("--auth-url", action="store_true", help="Imprime a URL de autorizacao (valida 5 min)")
    parser.add_argument("--exchange-code", metavar="CODE", help="Troca o code por access_token/refresh_token")
    parser.add_argument("--shop-id", metavar="SHOP_ID", help="shop_id que veio junto com o code na redirect_uri")
    parser.add_argument("--debug-pedido", metavar="ORDER_SN", help="Imprime o JSON completo de um pedido")
    parser.add_argument("--debug-escrow", metavar="ORDER_SN", help="Imprime o detalhe financeiro (escrow) de um pedido")
    parser.add_argument(
        "--debug-lista", metavar="DIAS", type=int, help="Imprime pedidos atualizados nos ultimos N dias (max 15)"
    )
    args = parser.parse_args()

    redirect_uri = os.environ["SHOPEE_REDIRECT_URI"]

    if args.auth_url:
        print(montar_url_autorizacao(_env_partner_id(), _env_partner_key(), redirect_uri, args.ambiente))
    elif args.exchange_code:
        if not args.shop_id:
            raise SystemExit("--shop-id e obrigatorio junto com --exchange-code (veio na URL de redirect)")
        tokens = trocar_code_por_token(
            args.loja, _env_partner_id(), _env_partner_key(), args.exchange_code, args.shop_id, args.ambiente
        )
        print(f"Tokens salvos em {_tokens_path(args.loja)}")
        print(json.dumps({k: v for k, v in tokens.items() if k != "access_token"}, indent=2))
    elif args.debug_pedido:
        cliente = ShopeeClient(args.loja, args.ambiente)
        pedido = cliente.obter_detalhe_pedido(args.debug_pedido)
        print(json.dumps(pedido, indent=2, ensure_ascii=False))
    elif args.debug_escrow:
        cliente = ShopeeClient(args.loja, args.ambiente)
        escrow = cliente.obter_escrow_detalhe(args.debug_escrow)
        print(json.dumps(escrow, indent=2, ensure_ascii=False))
    elif args.debug_lista:
        cliente = ShopeeClient(args.loja, args.ambiente)
        agora = int(time.time())
        lista = cliente.obter_lista_pedidos(agora - args.debug_lista * 86400, agora)
        print(json.dumps(lista, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
