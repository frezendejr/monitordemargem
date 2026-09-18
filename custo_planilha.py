"""Custo dos produtos vem de uma planilha de apoio mantida por fora do Tiny
(gerada por gerar_custos_por_codigo_pai.py e depois corrigida a mao pra
preencher os codigos sem custo encontrado), nao mais de consulta ao vivo na
API do Tiny. Motivo: o custo cadastrado no Tiny estava zerado/ausente numa
fatia grande dos produtos (ver README), e a planilha permite consolidar
custo de mais de uma fonte (Tiny + Mercado Livre) e corrigir manualmente o
que faltar sem depender de acesso de escrita ao Tiny.
"""

from __future__ import annotations

import logging
import os

import requests
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

COL_CUSTO = 2
COL_SKUS_DO_GRUPO = 5


def carregar_custos(caminho: str) -> dict[str, float]:
    """Le custo_por_codigo_pai.xlsx e devolve {sku: custo}.

    A planilha e por codigo_pai (uma linha por grupo de variacoes), com a
    coluna skus_do_grupo listando todos os SKUs daquele grupo separados por
    virgula - aqui expandimos isso pra um dict direto por SKU, que e o que
    aparece em item["codigo"] no pedido do Tiny.
    """
    wb = load_workbook(caminho, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    custos: dict[str, float] = {}
    linhas = ws.iter_rows(values_only=True)
    next(linhas)  # cabecalho: codigo_pai, descricao, custo, fonte, conflito, skus_do_grupo

    for row in linhas:
        custo = row[COL_CUSTO]
        skus_do_grupo = row[COL_SKUS_DO_GRUPO]
        if custo in (None, "") or not skus_do_grupo:
            continue
        for sku in str(skus_do_grupo).split(","):
            sku = sku.strip()
            if sku:
                custos[sku] = float(custo)

    return custos


_PAGINA = 1000


def sobrepor_custos_do_dashboard(custos: dict[str, float]) -> dict[str, float]:
    """Sobrepoe (in-place, e retorna) `custos` com os valores que estao em
    margin_monitor_custos no Supabase - tanto os migrados da planilha
    original (gerar_custos_para_supabase.py) quanto os que o time preencheu
    direto no dashboard. Esses ganham prioridade sobre a planilha local,
    ja que sao a fonte mais recente.

    Passar `custos={}` carrega SOMENTE do Supabase - e o que o
    monitor_cloud.py faz (sem planilha local disponivel no runner).

    Pagina em blocos de 1000 (limite padrao do Supabase) - a tabela ja
    passou de ~2000 linhas depois da migracao da planilha original.

    Falha de rede/credencial aqui NUNCA deve impedir o monitor de rodar -
    so loga e segue com o que ja tinha em `custos`.
    """
    try:
        url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}

        inicio = 0
        while True:
            resp = requests.get(
                f"{url}/rest/v1/margin_monitor_custos",
                params={"select": "sku,custo"},
                headers={**headers, "Range": f"{inicio}-{inicio + _PAGINA - 1}"},
                timeout=15,
            )
            resp.raise_for_status()
            pagina = resp.json()
            for linha in pagina:
                custos[linha["sku"]] = float(linha["custo"])
            if len(pagina) < _PAGINA:
                break
            inicio += _PAGINA
    except (KeyError, requests.RequestException):
        logger.warning("Nao foi possivel buscar custos do Supabase - usando so o que ja tinha")

    return custos
