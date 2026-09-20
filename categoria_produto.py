"""Categoria de produto (Calcados, Utensilios domesticos, Fitness, ...) -
resolvida automaticamente a partir da API do Mercado Livre, a pedido do
time (2026-09-19): o anuncio (MLB...) tem `category_id`, e a categoria em
si tem um `path_from_root` hierarquico (ex.: "Calcados, Roupas e Bolsas" >
"Calcados" > "Botas") - usa o 2o nivel desse caminho, que e o granularidade
que bate com como o time pensa em categoria (o 1o e grande demais, o
ultimo e fino demais). So funciona pra produtos que ja venderam pelo
Mercado Livre pelo menos uma vez (e o unico canal que da category_id) -
produto que so vendeu por Shopee/Amazon/TikTok Shop fica "Sem categoria"
ate vender pelo Meli tambem.

So o backend (monitor.py/monitor_cloud.py, service role) escreve na
tabela margin_monitor_categorias - o dashboard so le.
"""

from __future__ import annotations

import os

import requests

from ml_client import MLApiError, MLClient

SEM_CATEGORIA = "Sem categoria"

# O time pensa em poucos grupos grandes (Calcados, Utilidade Domestica,
# Fitness), nao na nomenclatura exata do Meli - essas 2 categorias de topo
# do Meli viram uma so a pedido do time (2026-09-19).
_RENOMEIA_CATEGORIA = {
    "Casa, Móveis e Decoração": "Utilidade Doméstica",
    "Construção": "Utilidade Doméstica",
    "Calçados, Roupas e Bolsas": "Calçados",
}


def _codigo_pai(sku: str) -> str:
    """Mesma convencao usada no dashboard (Amo Shoes: SKU = codigo_pai +
    tamanho de 2 digitos) - duplicada aqui de proposito, e um modulo de
    backend separado do dashboard.py (frontend)."""
    sku = str(sku).strip()
    if len(sku) > 2 and sku[:-2].isdigit() and sku[-2:].isdigit():
        return sku[:-2]
    return sku


def carregar_categorias() -> dict[str, str]:
    """{codigo_pai: categoria} - le tudo que ja foi classificado."""
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_ANON_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    categorias: dict[str, str] = {}
    inicio = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/margin_monitor_categorias",
            headers={**headers, "Range": f"{inicio}-{inicio + 999}"},
            params={"select": "codigo_pai,categoria"},
            timeout=30,
        )
        resp.raise_for_status()
        pagina = resp.json()
        categorias.update({r["codigo_pai"]: r["categoria"] for r in pagina})
        if len(pagina) < 1000:
            break
        inicio += 1000
    return categorias


def salvar_categoria(codigo_pai: str, categoria: str, category_id_ml: str | None) -> None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_categorias?on_conflict=codigo_pai",
        json={"codigo_pai": codigo_pai, "categoria": categoria, "category_id_ml": category_id_ml},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=15,
    )
    resp.raise_for_status()


def resolver_categoria_via_ml(
    canal: str, anuncio_id: str, ml_clientes: dict[str, MLClient]
) -> tuple[str, str] | None:
    """Retorna (nome_categoria, category_id) ou None se nao conseguir
    resolver (erro de API, item sem categoria, token de conta errada etc)
    - nunca levanta excecao, isso e enriquecimento, nao pode derrubar o
    ciclo do monitor."""
    try:
        if canal not in ml_clientes:
            ml_clientes[canal] = MLClient(canal)
        item = ml_clientes[canal].obter_item(anuncio_id)
        category_id = item.get("category_id")
        if not category_id:
            return None
        categoria_dados = ml_clientes[canal].obter_categoria(category_id)
        caminho = categoria_dados.get("path_from_root") or []
        # Usa o 1o nivel (topo da arvore do Meli, ex.: "Casa, Moveis e
        # Decoracao", "Esportes e Fitness") - testado contra os 98 codigo_pai
        # reais do catalogo em 2026-09-19: o 2o nivel espalhava produto de
        # casa em 8 categorias diferentes (Cozinha, Iluminacao Residencial,
        # Organizacao para Casa, Cuidado da Casa...), o que nao bate com o
        # jeito que o time pensa em categoria (poucos grupos grandes:
        # Calcados, Utensilios domesticos, Fitness) - o topo consolida tudo
        # isso corretamente.
        nome = caminho[0]["name"] if caminho else categoria_dados.get("name")
        if not nome:
            return None
        nome = _RENOMEIA_CATEGORIA.get(nome, nome)
        return (nome, category_id)
    except MLApiError:
        return None


def recalcular_categoria_pendente(limite: int = 20) -> int:
    """Acha codigo_pai com anuncio_id conhecido (so ML da isso) mas ainda
    sem categoria classificada, resolve via API do Meli (no maximo
    `limite` por chamada, pra nao estourar rate limit do Meli dentro de um
    unico ciclo de 15min do monitor) e grava. Retorna quantos foram
    classificados agora. Falha de rede/credencial aqui NUNCA deve impedir
    o ciclo do monitor de continuar (mesma regra de recalcular_custo_
    pendente em custo_planilha.py)."""
    try:
        url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    except KeyError:
        return 0

    try:
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}

        conhecidas = carregar_categorias()

        itens: list[dict] = []
        inicio = 0
        while True:
            resp = requests.get(
                f"{url}/rest/v1/margin_monitor_itens",
                headers={**headers, "Range": f"{inicio}-{inicio + 999}"},
                params={"select": "sku,canal,anuncio_id", "anuncio_id": "not.is.null"},
                timeout=30,
            )
            resp.raise_for_status()
            pagina = resp.json()
            itens.extend(pagina)
            if len(pagina) < 1000:
                break
            inicio += 1000

        pendentes: dict[str, tuple[str, str]] = {}
        for item in itens:
            pai = _codigo_pai(item["sku"])
            if pai not in conhecidas and pai not in pendentes:
                pendentes[pai] = (item["canal"], item["anuncio_id"])

        ml_clientes: dict[str, MLClient] = {}
        classificados = 0
        for codigo_pai, (canal, anuncio_id) in list(pendentes.items())[:limite]:
            resultado = resolver_categoria_via_ml(canal, anuncio_id, ml_clientes)
            if resultado is None:
                continue
            nome, category_id = resultado
            salvar_categoria(codigo_pai, nome, category_id)
            classificados += 1

        return classificados
    except requests.RequestException:
        return 0
