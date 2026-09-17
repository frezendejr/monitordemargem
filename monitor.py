"""Loop principal do monitor de margem de contribuicao.

O Grupo Amo tem 4 contas Tiny distintas (uma por CNPJ: amoshoesgyn, amoshoes,
inamorato e a do CNPJ 28849110000141), cada uma com seu proprio token e seus
proprios canais de venda (ver tiny_contas em config.yaml). Cada ciclo passa
por TODAS as contas, uma de cada vez.

Uso:
    python monitor.py --once   # roda um ciclo em todas as contas e sai
    python monitor.py          # roda em loop continuo (intervalo_minutos do config.yaml)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv

import storage
from alerts import atualizar_planilha, enviar_email, enviar_whatsapp, montar_mensagem_alerta
from margin_engine import calcular_margem
from ml_client import MLClient, MLApiError
from tiny_client import TinyClient, TinyApiError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("monitor")

CONFIG_PATH_PADRAO = "config.yaml"
JANELA_INICIAL = timedelta(hours=24)


def carregar_config(caminho: str = CONFIG_PATH_PADRAO) -> dict:
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolver_receita_e_config(
    pedido_completo: dict,
    canal: str,
    canal_config: dict,
    ml_clientes: dict[str, MLClient],
) -> tuple[float, dict]:
    """Decide a receita e o canal_config a usar no calculo da margem.

    Se o canal tiver `receita_exata_via_ml` (hoje so meli_conta_2), busca a
    receita liquida EXATA via Mercado Pago (ja descontando tarifa/frete real
    e considerando cupom reembolsado pelo ML - ver README) e zera
    comissao_pct/frete_pct pra nao descontar de novo por cima. Se a chamada
    falhar (token expirado, pedido nao encontrado, rede fora), cai pro
    calculo estimado normal em vez de travar o pedido inteiro.
    """
    if not canal_config.get("receita_exata_via_ml"):
        receita = float(pedido_completo.get("total_pedido", 0) or 0)
        return receita, canal_config

    numero_ecommerce = pedido_completo.get("numero_ecommerce")
    try:
        if canal not in ml_clientes:
            ml_clientes[canal] = MLClient(canal)
        receita_exata = ml_clientes[canal].obter_receita_liquida_pedido(numero_ecommerce)
    except MLApiError:
        logger.exception(
            "[%s] Falha ao buscar receita exata via ML pro pedido ML %s - usando estimativa por %%",
            canal,
            numero_ecommerce,
        )
        receita = float(pedido_completo.get("total_pedido", 0) or 0)
        return receita, canal_config

    canal_config_exato = dict(canal_config, comissao_pct=0.0, frete_pct=0.0)
    return receita_exata, canal_config_exato


def processar_ciclo_conta(
    conta_tiny: str,
    cliente: TinyClient,
    canais_config: dict,
    config: dict,
    conn,
) -> int:
    """Roda um ciclo para UMA conta Tiny: busca pedidos novos, calcula
    margem, alerta se preciso. Retorna a quantidade de pedidos processados.
    """
    ml_clientes: dict[str, MLClient] = {}
    checkpoint = storage.obter_checkpoint(conn, conta_tiny)
    desde = (
        datetime.fromisoformat(checkpoint)
        if checkpoint
        else datetime.now(timezone.utc) - JANELA_INICIAL
    )

    proxima_marca = datetime.now(timezone.utc)

    try:
        pedidos_resumo = cliente.buscar_pedidos_atualizados_desde(desde)
    except TinyApiError:
        logger.exception("[%s] Falha ao buscar pedidos atualizados no Tiny", conta_tiny)
        return 0

    logger.info(
        "[%s] %d pedido(s) atualizado(s) desde %s",
        conta_tiny,
        len(pedidos_resumo),
        desde.isoformat(),
    )

    custo_cache: dict[str, float | None] = {}
    processados = 0
    houve_falha_de_api = False

    for resumo in pedidos_resumo:
        # "id" e o identificador que pedido.obter.php espera (confirmado via
        # --debug-pedido); "numero" e so o numero de exibicao no Tiny. Ainda
        # NAO confirmado que pedidos.pesquisa.php devolve "id" no resumo -
        # rodar --debug-busca pra checar (ver README, item 3 da calibracao).
        id_tiny = str(resumo.get("id") or resumo.get("numero"))
        if not id_tiny or id_tiny == "None":
            continue
        if storage.ja_processado(conn, conta_tiny, id_tiny):
            continue

        try:
            pedido_completo = cliente.obter_pedido_completo(id_tiny)
        except TinyApiError:
            logger.exception("[%s] Falha ao obter detalhe do pedido %s", conta_tiny, id_tiny)
            # Nao avanca o checkpoint neste ciclo - um pedido nao obtido por
            # erro de API (ex.: rate limit) nao pode ser dado como "visto",
            # senao o filtro por data nunca mais o traria de volta.
            houve_falha_de_api = True
            continue

        canal = cliente.identificar_canal(pedido_completo, canais_config)
        if canal is None:
            logger.warning("[%s] Pedido %s sem canal identificado - pulando", conta_tiny, id_tiny)
            continue

        for wrapper in pedido_completo.get("itens", []):
            item = wrapper.get("item", wrapper)
            # produto.obter.php exige o id_produto interno do Tiny, nao o
            # codigo/SKU (ver tiny_client.py) - custo_cache e chaveado por ele.
            id_produto = str(item.get("id_produto"))
            if id_produto not in custo_cache:
                try:
                    custo_cache[id_produto] = cliente.obter_custo_produto(id_produto)
                except TinyApiError:
                    # Nao cacheia: um erro de API (rate limit, timeout) nao
                    # pode virar "custo ausente" permanente pro resto da
                    # rodada - deixa faltar so nesse item, e tenta de novo
                    # na proxima vez que esse produto aparecer.
                    logger.warning(
                        "[%s] Falha ao buscar custo do produto id=%s - tratando so este item como custo ausente",
                        conta_tiny,
                        id_produto,
                    )

        itens = cliente.montar_itens(pedido_completo, custo_cache)
        receita, canal_config_efetivo = _resolver_receita_e_config(
            pedido_completo, canal, canais_config[canal], ml_clientes
        )

        resultado = calcular_margem(
            numero_pedido=id_tiny,
            canal=canal,
            itens=itens,
            receita=receita,
            canal_config=canal_config_efetivo,
        )

        threshold = config.get("alertas", {}).get("margem_negativa_threshold", 0)
        deve_alertar = resultado.margem_contribuicao < threshold

        if deve_alertar:
            mensagem = montar_mensagem_alerta(resultado)
            enviar_whatsapp(mensagem, config)
            enviar_email(f"Margem negativa - pedido {id_tiny} ({conta_tiny})", mensagem, config)
            logger.warning("[%s] ALERTA margem negativa: pedido %s (%s)", conta_tiny, id_tiny, resultado.canal)

        atualizar_planilha(resultado)
        storage.salvar_pedido(conn, conta_tiny, resultado, alertado=deve_alertar)
        processados += 1

    if houve_falha_de_api:
        logger.warning(
            "[%s] Checkpoint NAO avancado por causa de falha de API neste ciclo - "
            "os pedidos que falharam serao tentados de novo no proximo ciclo",
            conta_tiny,
        )
    else:
        storage.salvar_checkpoint(conn, conta_tiny, proxima_marca.isoformat())

    return processados


def processar_ciclo(config: dict, conn) -> int:
    """Roda um ciclo em todas as contas Tiny cadastradas em config.yaml."""
    total = 0
    for conta_tiny, conta_cfg in config["tiny_contas"].items():
        token_env = conta_cfg["token_env"]
        token = os.environ.get(token_env)
        if not token:
            logger.error("[%s] Variavel %s vazia no .env - pulando conta", conta_tiny, token_env)
            continue

        cliente = TinyClient(token)
        total += processar_ciclo_conta(conta_tiny, cliente, conta_cfg["canais"], config, conn)

    return total


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Monitor de margem de contribuicao")
    parser.add_argument("--once", action="store_true", help="Roda um ciclo em todas as contas e sai")
    args = parser.parse_args()

    config = carregar_config()

    with storage.sessao() as conn:
        if args.once:
            n = processar_ciclo(config, conn)
            logger.info("Ciclo unico concluido: %d pedido(s) processado(s) no total", n)
            return

        intervalo = config.get("geral", {}).get("intervalo_minutos", 10)
        logger.info("Iniciando loop continuo (intervalo de %d min)", intervalo)
        while True:
            try:
                n = processar_ciclo(config, conn)
                logger.info("Ciclo concluido: %d pedido(s) processado(s) no total", n)
            except Exception:
                logger.exception("Erro inesperado no ciclo - continuando no proximo")
            time.sleep(intervalo * 60)


if __name__ == "__main__":
    main()
