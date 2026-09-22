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
import supabase_writer
from alerts import atualizar_planilha, enviar_email, enviar_whatsapp, montar_mensagem_alerta
from categoria_produto import recalcular_categoria_pendente
from custo_planilha import carregar_custos, recalcular_custo_pendente, sobrepor_custos_do_dashboard
from margin_engine import calcular_margem
from ml_client import DetalheFinanceiroPedido, MLApiError, MLClient
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
CUSTO_PLANILHA_PADRAO = "custo_por_codigo_pai.xlsx"
JANELA_INICIAL = timedelta(hours=24)


def carregar_config(caminho: str = CONFIG_PATH_PADRAO) -> dict:
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolver_receita_e_config(
    pedido_completo: dict,
    canal: str,
    canal_config: dict,
    ml_clientes: dict[str, MLClient],
) -> tuple[float, dict, DetalheFinanceiroPedido | None]:
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
        return receita, canal_config, None

    numero_ecommerce = pedido_completo.get("numero_ecommerce")
    try:
        if canal not in ml_clientes:
            ml_clientes[canal] = MLClient(canal)
        detalhe = ml_clientes[canal].obter_detalhe_financeiro_pedido(numero_ecommerce)
    except MLApiError:
        logger.exception(
            "[%s] Falha ao buscar receita exata via ML pro pedido ML %s - usando estimativa por %%",
            canal,
            numero_ecommerce,
        )
        receita = float(pedido_completo.get("total_pedido", 0) or 0)
        return receita, canal_config, None

    # faixas_frete tem prioridade sobre frete_pct em _percentual_frete - zerar
    # so o frete_pct nao bastava pro canal meli_conta_1 (usa faixas_frete),
    # entao o frete estimado continuava sendo descontado EM CIMA da receita
    # que ja vem liquida de frete real (bug real: confirmado contra a venda
    # 2000018537926936 em 2026-09-19, R$26,32 de frete cobrado em dobro).
    canal_config_exato = dict(canal_config, comissao_pct=0.0, frete_pct=0.0, faixas_frete=None)
    return detalhe.receita_liquida, canal_config_exato, detalhe


def processar_ciclo_conta(
    conta_tiny: str,
    cliente: TinyClient,
    canais_config: dict,
    config: dict,
    conn,
    custos: dict[str, float],
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
    except Exception:
        # Antes so pegava TinyApiError - mas um erro HTTP (resp.raise_for_status())
        # ou uma resposta que nao e JSON valido (resp.json()) sobe como
        # requests.HTTPError/JSONDecodeError, nao TinyApiError, e nao era pego
        # aqui. Ver o mesmo fix em monitor_cloud.py pro motivo completo.
        logger.exception("[%s] Falha ao buscar pedidos atualizados no Tiny", conta_tiny)
        return 0

    logger.info(
        "[%s] %d pedido(s) atualizado(s) desde %s",
        conta_tiny,
        len(pedidos_resumo),
        desde.isoformat(),
    )

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

        itens = cliente.montar_itens(pedido_completo, custos)
        receita, canal_config_efetivo, detalhe_ml = _resolver_receita_e_config(
            pedido_completo, canal, canais_config[canal], ml_clientes
        )

        resultado = calcular_margem(
            numero_pedido=id_tiny,
            canal=canal,
            itens=itens,
            receita=receita,
            canal_config=canal_config_efetivo,
            data_pedido=pedido_completo.get("data_pedido", ""),
        )

        # comissao/frete "reais" so pra exibicao detalhada no dashboard - nao
        # mudam margem_contribuicao (ja calculada acima com receita liquida
        # e comissao/frete zerados, ver _resolver_receita_e_config).
        if detalhe_ml is not None:
            valor_venda = detalhe_ml.valor_venda
            resultado.comissao = detalhe_ml.comissao_real
            resultado.frete = detalhe_ml.frete_real

            # Mercado Ads e cobrado a parte (fatura mensal), nao aparece em
            # nenhuma API do pedido - regra do time: 8% sobre o VALOR DE
            # VENDA bruto, nao sobre a receita liquida. calcular_margem
            # aplicou ads_pct em cima da receita liquida (a unica que ele
            # recebe) - corrige aqui pra usar a base certa e propaga a
            # diferenca pra margem_contribuicao/margem_pct, que ISSO SIM muda
            # de verdade (ads nao esta embutido na receita liquida do ML,
            # diferente de comissao/frete).
            ads_pct = canal_config_efetivo.get("ads_pct", 0.0)
            ads_correto = round(valor_venda * ads_pct / 100, 2)
            resultado.margem_contribuicao = round(resultado.margem_contribuicao - (ads_correto - resultado.ads), 2)
            resultado.margem_pct = round(resultado.margem_contribuicao / resultado.receita * 100, 2) if resultado.receita else 0.0
            resultado.ads = ads_correto
        else:
            valor_venda = resultado.receita

        threshold = config.get("alertas", {}).get("margem_negativa_threshold", 0)
        deve_alertar = resultado.margem_contribuicao < threshold

        if deve_alertar:
            mensagem = montar_mensagem_alerta(resultado)
            enviar_whatsapp(mensagem, config)
            enviar_email(f"Margem negativa - pedido {id_tiny} ({conta_tiny})", mensagem, config)
            logger.warning("[%s] ALERTA margem negativa: pedido %s (%s)", conta_tiny, id_tiny, resultado.canal)

        atualizar_planilha(resultado)
        storage.salvar_pedido(conn, conta_tiny, resultado, alertado=deve_alertar)

        try:
            supabase_writer.enviar_pedido(conta_tiny, resultado, alertado=deve_alertar, valor_venda=valor_venda)
            supabase_writer.enviar_itens(
                conta_tiny,
                resultado,
                anuncios=detalhe_ml.anuncios if detalhe_ml else None,
                numero_ecommerce=pedido_completo.get("numero_ecommerce"),
            )
        except (supabase_writer.SupabaseError, KeyError):
            # So alimenta o dashboard - nunca pode travar o monitor. O
            # SQLite local (fonte de verdade operacional) ja foi salvo acima.
            logger.exception("[%s] Falha ao gravar pedido %s no Supabase (dashboard)", conta_tiny, id_tiny)

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


def processar_ciclo(config: dict, conn, custo_planilha: str) -> int:
    """Roda um ciclo em todas as contas Tiny cadastradas em config.yaml.

    Recarrega a planilha de custo a cada ciclo (nao so uma vez no inicio),
    pra uma correcao manual feita nela valer a partir do proximo ciclo, sem
    precisar reiniciar o processo continuo.
    """
    custos = carregar_custos(custo_planilha)
    custos = sobrepor_custos_do_dashboard(custos)
    logger.info("Custos carregados (planilha + dashboard): %d SKU(s) com custo", len(custos))

    n_corrigidos = recalcular_custo_pendente()
    if n_corrigidos:
        logger.info("Custo pendente preenchido no dashboard: %d item(ns) recalculado(s)", n_corrigidos)

    total = 0
    for conta_tiny, conta_cfg in config["tiny_contas"].items():
        token_env = conta_cfg["token_env"]
        token = os.environ.get(token_env)
        if not token:
            logger.error("[%s] Variavel %s vazia no .env - pulando conta", conta_tiny, token_env)
            continue

        cliente = TinyClient(token)
        try:
            total += processar_ciclo_conta(conta_tiny, cliente, conta_cfg["canais"], config, conn, custos)
        except Exception:
            # Nenhuma conta pode travar as outras 3 no mesmo ciclo.
            logger.exception("[%s] Erro inesperado processando a conta - pulando pro proximo ciclo", conta_tiny)

    return total


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Monitor de margem de contribuicao")
    parser.add_argument("--once", action="store_true", help="Roda um ciclo em todas as contas e sai")
    parser.add_argument(
        "--custo-planilha",
        default=CUSTO_PLANILHA_PADRAO,
        help=f"Caminho da planilha de custo por SKU (padrao: {CUSTO_PLANILHA_PADRAO})",
    )
    args = parser.parse_args()

    config = carregar_config()

    with storage.sessao() as conn:
        if args.once:
            n = processar_ciclo(config, conn, args.custo_planilha)
            logger.info("Ciclo unico concluido: %d pedido(s) processado(s) no total", n)
            return

        intervalo = config.get("geral", {}).get("intervalo_minutos", 10)
        logger.info("Iniciando loop continuo (intervalo de %d min)", intervalo)
        while True:
            try:
                n = processar_ciclo(config, conn, args.custo_planilha)
                logger.info("Ciclo concluido: %d pedido(s) processado(s) no total", n)
            except Exception:
                logger.exception("Erro inesperado no ciclo - continuando no proximo")
            time.sleep(intervalo * 60)


if __name__ == "__main__":
    main()
