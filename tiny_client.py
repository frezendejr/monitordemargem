"""Cliente da API do Tiny ERP.

Escrito para a API v2 classica (autenticacao por token unico, formato=json).
Se a conta usar a v3 (OAuth2), __init__ e _post precisam mudar para o fluxo
OAuth2 (client_id/client_secret/refresh_token) - ver PROMPT_PARA_CLAUDE_CODE.md
passo 1.

CONFIRMADO contra pedidos e buscas reais das 4 contas Tiny (ver README,
secao "Avisos de campos ajustados"):
  - Endpoint de listagem: pedidos.pesquisa.php, filtro por dataAtualizacao
    (testado com --debug-busca, retornou resultados reais).
  - O resumo de pedidos.pesquisa.php ja traz `id` (usado por
    pedido.obter.php) e `numero` (numero de exibicao) juntos.
  - Endpoint de detalhe: pedido.obter.php (parametro `id`, retorna itens).
  - Endpoint de produto: produto.obter.php - **parametro `id`** (o
    id_produto interno do Tiny, ex.: item["id_produto"] do pedido), **NAO
    `codigo`** (SKU) apesar do nome do campo sugerir o contrario. Passar
    `codigo` retorna erro "O parametro id deve ser informado" e faz
    obter_custo_produto devolver None pra TODO produto - foi um bug real
    encontrado calibrando contra a conta real (custo_ausente aparecia em
    ~100% dos pedidos por causa disso, nao porque o cadastro estava vazio).
  - Campo de custo do produto: `preco_custo`/`preco_custo_medio` -
    confirmados, ambos populados (ex.: produto id=898908943 tinha
    preco_custo=73.49).
  - Campo de canal do pedido: `pedido["ecommerce"]["nomeEcommerce"]` (campo
    aninhado, nao um campo solto no nivel raiz como se assumia antes).
  - Receita do pedido: `pedido["total_pedido"]` (ja liquido do desconto que o
    Tiny enxergar - ver margin_engine.py).
  - Tarifa/comissao real, frete real cobrado do vendedor e estornos de
    cupom/bonus NAO aparecem em lugar nenhum do JSON do pedido - so existem
    no extrato de cada marketplace. imposto_pct/comissao_pct/frete_pct/
    ads_pct em config.yaml sao sempre estimativas (exceto quando
    receita_exata_via_ml esta ligado - ver ml_client.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
import yaml
from dotenv import load_dotenv

from margin_engine import ItemPedido

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tiny.com.br/api2"


class TinyApiError(RuntimeError):
    """Erro retornado pela API do Tiny (retorno.status != 'OK')."""


class TinyClient:
    def __init__(self, token: str):
        """`token` e sempre explicito: o Grupo Amo tem uma conta Tiny por
        CNPJ, cada uma com seu proprio token (ver tiny_contas em
        config.yaml) - nao ha um TINY_API_TOKEN unico global."""
        if not token:
            raise ValueError("token da conta Tiny nao informado")
        self.token = token
        self._sessao = requests.Session()

    def _post(self, endpoint: str, params: dict | None = None) -> dict:
        payload = {"token": self.token, "formato": "json"}
        payload.update(params or {})

        resp = self._sessao.post(f"{BASE_URL}/{endpoint}", data=payload, timeout=30)
        resp.raise_for_status()
        dados = resp.json()

        retorno = dados.get("retorno", {})
        if retorno.get("status") != "OK":
            raise TinyApiError(
                f"{endpoint}: status={retorno.get('status')} "
                f"erros={retorno.get('erros')}"
            )
        return retorno

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------

    def buscar_pedidos_atualizados_desde(self, desde: datetime) -> list[dict]:
        """Lista pedidos (resumo) atualizados a partir de `desde`.

        Assume o parametro `dataAtualizacao` (formato DD/MM/AAAA HH:MM:SS) no
        endpoint pedidos.pesquisa.php. Se a API real nao aceitar esse filtro
        (algumas contas so tem dataInicial/dataFinal), trocar aqui mantendo o
        contrato de retorno: lista de pedidos resumo com pelo menos `numero`.
        """
        pedidos: list[dict] = []
        pagina = 1
        while True:
            retorno = self._post(
                "pedidos.pesquisa.php",
                {
                    "dataAtualizacao": desde.strftime("%d/%m/%Y %H:%M:%S"),
                    "pagina": pagina,
                },
            )
            lote = [p["pedido"] for p in retorno.get("pedidos", [])]
            pedidos.extend(lote)

            numero_paginas = int(retorno.get("numero_paginas", 1))
            if pagina >= numero_paginas or not lote:
                break
            pagina += 1

        return pedidos

    def obter_pedido_completo(self, numero_ou_id: str) -> dict:
        retorno = self._post("pedido.obter.php", {"id": numero_ou_id})
        return retorno["pedido"]

    def identificar_canal(self, pedido: dict, canais_config: dict) -> str | None:
        """Casa um pedido do Tiny com uma chave de `canais` do config.yaml.

        Confirmado via --debug-pedido contra pedidos reais das 4 contas: o
        campo que identifica o canal e `ecommerce.nomeEcommerce` (aninhado,
        nao um campo solto no nivel raiz do pedido). Para contas de Mercado
        Livre esse valor ja vem com a subconta embutida (ex.:
        "ML_AMOSHOESEIRELI 2", "ML_AMOOUTLET 1"), entao nao ha ambiguidade
        entre Meli Conta 2 e Meli Conta 3 dentro da mesma conta Tiny - so
        precisa bater tiny_identificador com esse valor exato. Shopee, Amazon
        e TikTok Shop vem com o nome generico do marketplace ("Shopee",
        "Amazon", "TikTok Shop").
        """
        nome_canal = (pedido.get("ecommerce") or {}).get("nomeEcommerce")

        for chave, cfg in canais_config.items():
            if cfg.get("tiny_identificador") == nome_canal:
                return chave

        logger.warning(
            "Nenhum canal em config.yaml bate com ecommerce.nomeEcommerce=%r "
            "para o pedido %s (identificadores configurados: %s)",
            nome_canal,
            pedido.get("numero"),
            [cfg.get("tiny_identificador") for cfg in canais_config.values()],
        )
        return None

    @staticmethod
    def montar_itens(pedido_completo: dict, custo_por_sku: dict[str, float | None]) -> list[ItemPedido]:
        """`custo_por_sku` e chaveado por `codigo` (SKU) - vem da planilha de
        apoio (custo_planilha.py), nao mais de consulta ao vivo no Tiny (ver
        README: o custo cadastrado no Tiny estava zerado/ausente demais)."""
        itens_raw = pedido_completo.get("itens", [])
        itens = []
        for wrapper in itens_raw:
            item = wrapper.get("item", wrapper)
            sku = str(item.get("codigo") or item.get("id_produto"))
            itens.append(
                ItemPedido(
                    sku=sku,
                    quantidade=float(item.get("quantidade", 0)),
                    valor_unitario=float(item.get("valor_unitario", 0)),
                    custo_unitario=custo_por_sku.get(sku),
                )
            )
        return itens

    # ------------------------------------------------------------------
    # Produtos
    # ------------------------------------------------------------------

    def obter_produto(self, id_produto: str) -> dict:
        """produto.obter.php espera o parametro `id` (id interno do Tiny,
        ex.: item["id_produto"] do pedido) - confirmado contra a API real.
        `codigo` (SKU) NAO funciona nesse endpoint, apesar do nome sugestivo
        (retorna "O parametro id deve ser informado")."""
        retorno = self._post("produto.obter.php", {"id": id_produto})
        return retorno["produto"]

    def obter_custo_produto(self, id_produto: str) -> float | None:
        """Retorna o custo cadastrado do produto (por id_produto), ou None
        se o cadastro existir mas o custo estiver zerado/ausente.

        Propositalmente NAO engole TinyApiError aqui (deixa propagar) - um
        erro de API (rate limit, produto realmente inexistente, timeout) e
        diferente de "produto existe mas custo esta zerado", e o chamador
        (monitor.py) precisa saber a diferenca pra nao cachear um erro
        temporario como se fosse ausencia de custo permanente (bug real
        encontrado calibrando: um rate limit no meio de uma rodada grande
        marcava o produto como custo_ausente pro resto da execucao, mesmo
        ele tendo custo cadastrado).

        custo_ausente e proposital (ver 'Coisas para NAO fazer' no PROMPT):
        nao inventar/estimar custo quando o cadastro do Tiny esta
        genuinamente zerado.
        """
        produto = self.obter_produto(id_produto)

        custo = produto.get("preco_custo") or produto.get("preco_custo_medio")
        try:
            custo = float(custo)
        except (TypeError, ValueError):
            return None
        return custo if custo > 0 else None


# ----------------------------------------------------------------------
# CLI de depuracao (passo 2 do PROMPT_PARA_CLAUDE_CODE.md)
# ----------------------------------------------------------------------

def _resolver_token_da_conta(conta: str, config_path: str = "config.yaml") -> str:
    """Le config.yaml, acha `conta` em tiny_contas e retorna o valor da env
    var indicada em token_env (ex.: TINY_API_TOKEN_AMOSHOES)."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    contas = config.get("tiny_contas", {})
    if conta not in contas:
        disponiveis = ", ".join(contas.keys())
        raise SystemExit(f"Conta '{conta}' nao existe em config.yaml. Disponiveis: {disponiveis}")

    token_env = contas[conta]["token_env"]
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(f"Variavel {token_env} nao esta preenchida no .env")
    return token


def _main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Depuracao do cliente Tiny ERP")
    parser.add_argument(
        "--conta",
        required=True,
        metavar="CHAVE",
        help="Chave da conta em config.yaml (tiny_contas), ex.: conta_amoshoes",
    )
    parser.add_argument("--debug-pedido", metavar="NUMERO", help="Imprime o JSON completo de um pedido")
    parser.add_argument(
        "--debug-produto",
        metavar="ID_PRODUTO",
        help="Imprime o JSON completo de um produto (id_produto interno do Tiny, nao o SKU/codigo)",
    )
    parser.add_argument(
        "--debug-busca",
        metavar="DIAS",
        type=int,
        help="Lista pedidos atualizados nos ultimos N dias (testa pedidos.pesquisa.php)",
    )
    args = parser.parse_args()

    token = _resolver_token_da_conta(args.conta)
    cliente = TinyClient(token)

    if args.debug_pedido:
        pedido = cliente.obter_pedido_completo(args.debug_pedido)
        print(json.dumps(pedido, indent=2, ensure_ascii=False))
    elif args.debug_produto:
        produto = cliente.obter_produto(args.debug_produto)
        print(json.dumps(produto, indent=2, ensure_ascii=False))
    elif args.debug_busca is not None:
        desde = datetime.now(timezone.utc) - timedelta(days=args.debug_busca)
        pedidos = cliente.buscar_pedidos_atualizados_desde(desde)
        print(f"{len(pedidos)} pedido(s) desde {desde.isoformat()}\n")
        print(json.dumps(pedidos, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
