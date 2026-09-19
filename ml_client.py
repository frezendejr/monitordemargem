"""Cliente da API do Mercado Livre (OAuth2 + receita liquida exata por pedido).

Complementa o tiny_client.py: pros canais de Mercado Livre onde ja fizemos a
autorizacao OAuth (hoje so Meli Conta 2 - ver config.yaml, campo
`ml_tokens_path` de cada canal meli_*), o monitor consulta a API do ML +
Mercado Pago pra pegar a receita liquida EXATA do pedido
(transaction_details.net_received_amount), em vez de estimar comissao/frete
por %. Confirmado contra o extrato real de um pedido (ver README).

Uma unica aplicacao (ML_APP_ID/ML_CLIENT_SECRET) e reaproveitada pras 4
contas Meli - so o fluxo de autorizacao (passos 1-3 abaixo) precisa ser
repetido uma vez por conta, gerando um arquivo de tokens proprio pra cada uma.

Fluxo de autorizacao (manual, uma vez por conta Meli):
    python ml_client.py --conta meli_conta_2 --auth-url
    # abrir a URL impressa, logar como ADMIN da conta Meli em questao, autorizar
    # copiar o "code" da URL de redirect (mesmo que ela de 404)
    python ml_client.py --conta meli_conta_2 --exchange-code SEU_CODE
    # salva o access_token/refresh_token em ml_tokens_meli_conta_2.json

Depois disso, os metodos de leitura renovam o access_token sozinhos quando
necessario (usam e persistem o refresh_token, que e de uso unico).
"""

from __future__ import annotations

import argparse
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
class DetalheFinanceiroPedido:
    receita_liquida: float
    valor_venda: float
    comissao_real: float
    frete_real: float
    anuncios: dict[str, str] = None  # seller_sku -> id do anuncio (ex.: "MLB2048260121")

BASE_URL = "https://api.mercadolibre.com"
MP_BASE_URL = "https://api.mercadopago.com"
AUTH_URL = "https://auth.mercadolivre.com.br/authorization"


class MLApiError(RuntimeError):
    """Erro retornado pela API do Mercado Livre/Mercado Pago."""


def montar_url_autorizacao(app_id: str, redirect_uri: str) -> str:
    return f"{AUTH_URL}?response_type=code&client_id={app_id}&redirect_uri={redirect_uri}"


def _tokens_path(conta: str) -> Path:
    return Path(f"ml_tokens_{conta}.json")


def _modo_nuvem() -> bool:
    """Em ambiente sem disco persistente (GitHub Actions), os tokens tem
    que ir pro Supabase - senao o refresh_token (uso unico) se perde entre
    execucoes e a autorizacao inteira quebra depois de 1 rodada. Ativado
    via MARGIN_MONITOR_CLOUD=1 (setado no workflow do GitHub Actions)."""
    return os.environ.get("MARGIN_MONITOR_CLOUD") == "1"


def _salvar_tokens(conta: str, dados: dict) -> None:
    dados = dict(dados)
    dados["obtido_em"] = time.time()
    if _modo_nuvem():
        _salvar_tokens_supabase(conta, dados)
    else:
        _tokens_path(conta).write_text(json.dumps(dados, indent=2), encoding="utf-8")


def _carregar_tokens(conta: str) -> dict | None:
    if _modo_nuvem():
        return _carregar_tokens_supabase(conta)
    path = _tokens_path(conta)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _salvar_tokens_supabase(conta: str, dados: dict) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    payload = {
        "conta": conta,
        "access_token": dados["access_token"],
        "refresh_token": dados["refresh_token"],
        "expires_in": dados["expires_in"],
        "obtido_em": dados["obtido_em"],
    }
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_ml_tokens?on_conflict=conta",
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


def _carregar_tokens_supabase(conta: str) -> dict | None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_ml_tokens",
        params={"conta": f"eq.{conta}", "select": "*"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    linhas = resp.json()
    return linhas[0] if linhas else None


def trocar_code_por_token(conta: str, app_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/oauth/token",
        headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if not resp.ok:
        raise MLApiError(f"Falha ao trocar code por token: {resp.status_code} {resp.text}")

    tokens = resp.json()
    _salvar_tokens(conta, tokens)
    return tokens


def _renovar_com_refresh_token(conta: str, app_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/oauth/token",
        headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": app_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if not resp.ok:
        raise MLApiError(
            f"Falha ao renovar token de '{conta}' (refresh_token pode ter expirado - "
            f"repetir --auth-url/--exchange-code pra essa conta): {resp.status_code} {resp.text}"
        )

    tokens = resp.json()
    _salvar_tokens(conta, tokens)
    return tokens


def _env_app_id(conta: str) -> str:
    return os.environ[f"ML_APP_ID_{conta.upper()}"]


def _env_client_secret(conta: str) -> str:
    return os.environ[f"ML_CLIENT_SECRET_{conta.upper()}"]


class MLClient:
    def __init__(self, conta: str, app_id: str | None = None, client_secret: str | None = None):
        """`conta` e a chave do canal em config.yaml (ex.: "meli_conta_2") -
        define tanto o arquivo de tokens quanto o app OAuth usado (cada
        conta Meli tem seu proprio app/Client ID/Client Secret, ver .env)."""
        self.conta = conta
        self.app_id = app_id or _env_app_id(conta)
        self.client_secret = client_secret or _env_client_secret(conta)

    def _access_token_valido(self) -> str:
        tokens = _carregar_tokens(self.conta)
        if not tokens:
            raise MLApiError(
                f"Nenhum token salvo pra '{self.conta}' - rodar --auth-url e --exchange-code primeiro"
            )

        # access_token expira em expires_in segundos (6h) a partir de obtido_em.
        # Renova com uma margem de seguranca de 5 minutos.
        expira_em = tokens["obtido_em"] + tokens["expires_in"] - 300
        if time.time() >= expira_em:
            logger.info("[%s] Access token expirado/perto de expirar - renovando", self.conta)
            tokens = _renovar_com_refresh_token(self.conta, self.app_id, self.client_secret, tokens["refresh_token"])

        return tokens["access_token"]

    def obter_pedido(self, order_id: str) -> dict:
        access_token = self._access_token_valido()
        resp = requests.get(
            f"{BASE_URL}/orders/{order_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if not resp.ok:
            raise MLApiError(f"Falha ao obter pedido {order_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_pagamento(self, payment_id: str) -> dict:
        """Detalhe do pagamento via Mercado Pago (mesmo token do ML no
        Brasil) - onde a tarifa e o estorno exatos aparecem
        (transaction_details.net_received_amount)."""
        access_token = self._access_token_valido()
        resp = requests.get(
            f"{MP_BASE_URL}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if not resp.ok:
            raise MLApiError(f"Falha ao obter pagamento {payment_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_pack(self, pack_id: str) -> dict:
        """Um "pack" agrupa varios `orders` do ML que saem no mesmo envio
        (compra combinada) - a Tiny as vezes guarda o pack_id em
        numero_ecommerce em vez do order_id de verdade, e /orders/{id} da
        404 pra esse numero (confirmado: pack 2000015106907205 continha o
        order real 2000018537732164, total R$59,99 - batia exato com o
        total_pedido ja gravado)."""
        access_token = self._access_token_valido()
        resp = requests.get(
            f"{BASE_URL}/packs/{pack_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if not resp.ok:
            raise MLApiError(f"Falha ao obter pack {pack_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_envio(self, shipping_id: str) -> dict:
        """Detalhe do envio (Mercado Envios) - onde o frete REAL cobrado do
        vendedor aparece em shipping_option.list_cost (confirmado contra o
        pedido real 2000018537926936: list_cost=27.05, extrato mostrava
        "Envios: -R$27,05" - o campo `cost` e o que o COMPRADOR pagou, quase
        sempre 0 em frete gratis, nao serve pra isso)."""
        access_token = self._access_token_valido()
        resp = requests.get(
            f"{BASE_URL}/shipments/{shipping_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if not resp.ok:
            raise MLApiError(f"Falha ao obter envio {shipping_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_item(self, item_id: str) -> dict:
        """Detalhe do anuncio (MLB...) - usado so pra pegar category_id.
        Precisa de token (o endpoint bloqueia acesso anonimo com 403
        "PA_UNAUTHORIZED_RESULT_FROM_POLICIES", confirmado real em
        2026-09-19), e precisa ser o token da MESMA subconta dona do
        anuncio - usar token de outra subconta Meli tambem da 403."""
        access_token = self._access_token_valido()
        resp = requests.get(
            f"{BASE_URL}/items/{item_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"attributes": "id,category_id"},
            timeout=15,
        )
        if not resp.ok:
            raise MLApiError(f"Falha ao obter item {item_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_categoria(self, category_id: str) -> dict:
        """Detalhe da categoria (nome + path_from_root, a hierarquia
        completa) - endpoint publico do Meli, nao precisa de token."""
        resp = requests.get(f"{BASE_URL}/categories/{category_id}", timeout=15)
        if not resp.ok:
            raise MLApiError(f"Falha ao obter categoria {category_id}: {resp.status_code} {resp.text}")
        return resp.json()

    def obter_receita_liquida_pedido(self, ml_order_id: str) -> float:
        """Soma o net_received_amount de todos os pagamentos aprovados do
        pedido - o valor que a Amo efetivamente recebe, ja liquido de
        tarifa/frete real e de qualquer cupom subsidiado pelo ML."""
        return self.obter_detalhe_financeiro_pedido(ml_order_id).receita_liquida

    def obter_detalhe_financeiro_pedido(self, ml_order_id: str) -> "DetalheFinanceiroPedido":
        """Detalhe financeiro exato do pedido (nao estimado por %):

        - valor_venda: soma de unit_price*quantity dos itens (preco de
          tabela, o que aparece como "Preco do produto" no extrato do ML).
        - comissao_real: soma de order_items[].sale_fee - confirmado que bate
          exatamente com "Tarifa de venda total" do extrato (pedido real
          2000018537926936: sale_fee=47.50, extrato mostrava -R$47,50).
        - receita_liquida: soma do net_received_amount dos pagamentos
          aprovados - o que a Amo recebe de fato.
        - frete_real: shipping_option.list_cost do envio (endpoint
          /shipments/{id}, via `shipping.id` do pedido) - confirmado que
          bate exatamente com "Envios" do extrato (pedido real
          2000018537926936: list_cost=27.05, extrato mostrava -R$27,05). Se
          o pedido nao tiver envio (retirada em loja, etc.) ou a chamada
          falhar, cai pro calculo por diferenca
          (valor_venda - comissao_real - receita_liquida) como aproximacao.

        Se `ml_order_id` na verdade for um pack_id (a Tiny guarda isso em
        numero_ecommerce pra algumas compras combinadas - /orders/{id} da
        404 "Order do not exists" nesse caso), busca /packs/{id} e soma os
        `orders` de dentro dele em vez de um pedido so.
        """
        try:
            pedidos_ml = [self.obter_pedido(ml_order_id)]
        except MLApiError:
            pack = self.obter_pack(ml_order_id)
            pedidos_ml = [self.obter_pedido(str(o["id"])) for o in pack.get("orders", [])]

        valor_venda = 0.0
        comissao_real = 0.0
        receita_liquida = 0.0
        shipping_id = None
        anuncios: dict[str, str] = {}
        for pedido_ml in pedidos_ml:
            itens = pedido_ml.get("order_items", [])
            valor_venda += sum(item["unit_price"] * item["quantity"] for item in itens)
            comissao_real += sum(item.get("sale_fee") or 0.0 for item in itens)
            for order_item in itens:
                item_info = order_item.get("item") or {}
                seller_sku = item_info.get("seller_sku")
                anuncio_id = item_info.get("id")
                if seller_sku and anuncio_id:
                    anuncios[str(seller_sku)] = str(anuncio_id)
            for pagamento_resumo in pedido_ml.get("payments", []):
                if pagamento_resumo.get("status") != "approved":
                    continue
                pagamento = self.obter_pagamento(str(pagamento_resumo["id"]))
                receita_liquida += pagamento["transaction_details"]["net_received_amount"]
            if shipping_id is None:
                shipping_id = (pedido_ml.get("shipping") or {}).get("id")

        frete_real = round(valor_venda - comissao_real - receita_liquida, 2)
        if shipping_id:
            try:
                envio = self.obter_envio(str(shipping_id))
                list_cost = (envio.get("shipping_option") or {}).get("list_cost")
                if list_cost is not None:
                    frete_real = round(float(list_cost), 2)
            except MLApiError:
                logger.warning(
                    "Falha ao buscar envio %s do pedido %s - usando frete por diferenca (%.2f)",
                    shipping_id,
                    ml_order_id,
                    frete_real,
                )

        return DetalheFinanceiroPedido(
            receita_liquida=round(receita_liquida, 2),
            valor_venda=round(valor_venda, 2),
            comissao_real=round(comissao_real, 2),
            frete_real=frete_real,
            anuncios=anuncios,
        )


def _main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Depuracao do cliente Mercado Livre")
    parser.add_argument("--conta", required=True, metavar="CHAVE", help="Chave do canal Meli, ex.: meli_conta_2")
    parser.add_argument("--auth-url", action="store_true", help="Imprime a URL de autorizacao")
    parser.add_argument("--exchange-code", metavar="CODE", help="Troca o code por access_token/refresh_token")
    parser.add_argument("--debug-order", metavar="ORDER_ID", help="Imprime o JSON completo de um pedido")
    parser.add_argument("--debug-payment", metavar="PAYMENT_ID", help="Imprime o detalhe de um pagamento (Mercado Pago)")
    parser.add_argument("--debug-receita", metavar="ORDER_ID", help="Imprime a receita liquida exata de um pedido")
    args = parser.parse_args()

    redirect_uri = os.environ["ML_REDIRECT_URI"]

    if args.auth_url:
        print(montar_url_autorizacao(_env_app_id(args.conta), redirect_uri))
    elif args.exchange_code:
        tokens = trocar_code_por_token(
            args.conta, _env_app_id(args.conta), _env_client_secret(args.conta), args.exchange_code, redirect_uri
        )
        print(f"Tokens salvos em {_tokens_path(args.conta)}")
        print(json.dumps({k: v for k, v in tokens.items() if k != "access_token"}, indent=2))
    elif args.debug_order:
        cliente = MLClient(args.conta)
        pedido = cliente.obter_pedido(args.debug_order)
        print(json.dumps(pedido, indent=2, ensure_ascii=False))
    elif args.debug_payment:
        cliente = MLClient(args.conta)
        pagamento = cliente.obter_pagamento(args.debug_payment)
        print(json.dumps(pagamento, indent=2, ensure_ascii=False))
    elif args.debug_receita:
        cliente = MLClient(args.conta)
        receita = cliente.obter_receita_liquida_pedido(args.debug_receita)
        print(f"Receita liquida real do pedido {args.debug_receita}: R$ {receita:.2f}")
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
