"""Versao do monitor pensada pra rodar num ambiente SEM disco persistente
(GitHub Actions) - sem SQLite local, sem loop continuo (o agendamento e
externo, via cron do GitHub Actions). Reusa toda a logica de negocio
(TinyClient, MLClient, margin_engine, alerts) do monitor.py normal; so troca
onde fica o dedupe/checkpoint (storage_supabase em vez de storage) e o custo
(direto do Supabase em vez da planilha local).

MARGIN_MONITOR_CLOUD=1 tem que estar setado (ml_client.py usa isso pra saber
que precisa persistir o token OAuth do Mercado Livre no Supabase em vez de
arquivo local - ver ml_client.py).

Nao escreve dashboard_margem.xlsx (nao faz sentido num runner efemero - o
dashboard hospedado no Supabase/Streamlit ja cobre esse caso de uso).

Uso: python monitor_cloud.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv

import storage_supabase as storage
import supabase_writer
from alerts import enviar_email, enviar_whatsapp, montar_mensagem_alerta
from categoria_produto import recalcular_categoria_pendente
from custo_planilha import recalcular_custo_pendente, sobrepor_custos_do_dashboard
from margin_engine import calcular_margem
from ml_client import MLApiError, MLClient
from tiny_client import TinyApiError, TinyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("monitor_cloud")

CONFIG_PATH_PADRAO = "config.yaml"


def carregar_config(caminho: str = CONFIG_PATH_PADRAO) -> dict:
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolver_receita_e_config(pedido_completo, canal, canal_config, ml_clientes):
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


def processar_conta(conta_tiny: str, cliente: TinyClient, canais_config: dict, config: dict, custos: dict) -> int:
    ml_clientes: dict[str, MLClient] = {}
    checkpoint = storage.obter_checkpoint(conta_tiny)
    desde = datetime.fromisoformat(checkpoint) if checkpoint else datetime.now(timezone.utc) - timedelta(hours=24)
    proxima_marca = datetime.now(timezone.utc)

    try:
        pedidos_resumo = cliente.buscar_pedidos_atualizados_desde(desde)
    except Exception:
        # Antes so pegava TinyApiError - mas um erro HTTP (resp.raise_for_status())
        # ou uma resposta que nao e JSON valido (resp.json()) sobe como
        # requests.HTTPError/JSONDecodeError, nao TinyApiError, e nao era pego
        # aqui. Isso derrubava o main() inteiro (todas as 4 contas) por causa
        # de UMA conta - confirmado como a causa provavel do monitor ficar
        # parado no GitHub Actions em 2026-09-19 (falha rapida, ~24s, tipica
        # de crash logo no inicio do ciclo, antes de processar qualquer coisa).
        logger.exception("[%s] Falha ao buscar pedidos atualizados no Tiny", conta_tiny)
        return 0

    logger.info("[%s] %d pedido(s) atualizado(s) desde %s", conta_tiny, len(pedidos_resumo), desde.isoformat())

    processados = 0
    houve_falha_de_api = False

    for resumo in pedidos_resumo:
        id_tiny = str(resumo.get("id") or resumo.get("numero"))
        if not id_tiny or id_tiny == "None":
            continue
        if storage.ja_processado(conta_tiny, id_tiny):
            continue

        try:
            pedido_completo = cliente.obter_pedido_completo(id_tiny)
        except TinyApiError:
            logger.exception("[%s] Falha ao obter detalhe do pedido %s", conta_tiny, id_tiny)
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

        try:
            supabase_writer.enviar_pedido(conta_tiny, resultado, alertado=deve_alertar, valor_venda=valor_venda)
            supabase_writer.enviar_itens(
                conta_tiny,
                resultado,
                anuncios=detalhe_ml.anuncios if detalhe_ml else None,
                numero_ecommerce=pedido_completo.get("numero_ecommerce"),
            )
        except supabase_writer.SupabaseError:
            # Diferente do monitor.py local: aqui o Supabase E o dedupe (nao
            # tem SQLite de apoio), entao uma falha aqui significa que esse
            # pedido sera tentado de novo no proximo ciclo (correto - nao
            # marca como processado se nao conseguiu gravar).
            logger.exception("[%s] Falha ao gravar pedido %s no Supabase - sera tentado de novo", conta_tiny, id_tiny)
            houve_falha_de_api = True
            continue

        processados += 1

    if houve_falha_de_api:
        logger.warning("[%s] Checkpoint NAO avancado por causa de falha nesse ciclo", conta_tiny)
    else:
        storage.salvar_checkpoint(conta_tiny, proxima_marca.isoformat())

    return processados


def main():
    load_dotenv()
    if os.environ.get("MARGIN_MONITOR_CLOUD") != "1":
        logger.warning("MARGIN_MONITOR_CLOUD nao esta setado como '1' - os tokens do ML nao vao persistir certo")

    config = carregar_config()
    custos = sobrepor_custos_do_dashboard({})
    logger.info("Custos carregados do Supabase: %d SKU(s)", len(custos))

    n_corrigidos = recalcular_custo_pendente()
    if n_corrigidos:
        logger.info("Custo pendente preenchido no dashboard: %d item(ns) recalculado(s)", n_corrigidos)

    n_categorias = recalcular_categoria_pendente()
    if n_categorias:
        logger.info("Categoria de produto classificada via API do Meli: %d codigo(s) pai", n_categorias)

    total = 0
    for conta_tiny, conta_cfg in config["tiny_contas"].items():
        token_env = conta_cfg["token_env"]
        token = os.environ.get(token_env)
        if not token:
            logger.error("[%s] Variavel %s vazia - pulando conta", conta_tiny, token_env)
            continue

        cliente = TinyClient(token)
        try:
            total += processar_conta(conta_tiny, cliente, conta_cfg["canais"], config, custos)
        except Exception:
            # Rede de seguranca extra: qualquer erro nao previsto numa conta
            # (aqui ou dentro de processar_conta) nunca pode impedir as
            # outras 3 contas de rodar no mesmo ciclo.
            logger.exception("[%s] Erro inesperado processando a conta - pulando pro proximo ciclo", conta_tiny)

    logger.info("Ciclo concluido: %d pedido(s) processado(s) no total", total)


if __name__ == "__main__":
    main()
