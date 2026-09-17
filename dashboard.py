"""Dashboard de margem de contribuicao - le do Supabase (tabelas
margin_monitor_pedidos e margin_monitor_itens, escritas por monitor.py via
supabase_writer.py).

Rodar local:
    streamlit run dashboard.py
(precisa de SUPABASE_URL e SUPABASE_ANON_KEY no .env)

Publicado no Streamlit Community Cloud, as mesmas 2 variaveis vao em
"Secrets" do app (nao no .env, que nao sobe pro git).
"""

from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Margem Grupo Amo", layout="wide")


def _config(chave: str) -> str:
    """Le de st.secrets (deploy) ou do .env (local).

    st.secrets lanca excecao so de ser acessado (nem precisa fazer lookup de
    chave) quando nao existe NENHUM secrets.toml - o que e o caso normal
    rodando local via `streamlit run` - por isso o acesso a st.secrets fica
    protegido, senao o fallback pro .env nunca seria alcancado.
    """
    try:
        if chave in st.secrets:
            return st.secrets[chave]
    except Exception:
        pass
    return os.environ[chave]


@st.cache_data(ttl=120)
def carregar_tabela(nome: str) -> pd.DataFrame:
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.get(
        f"{url}/rest/v1/{nome}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"select": "*", "order": "processado_em.desc", "limit": "20000"},
        timeout=30,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


st.title("Margem de contribuicao - Grupo Amo")

try:
    pedidos = carregar_tabela("margin_monitor_pedidos")
    itens = carregar_tabela("margin_monitor_itens")
except Exception as e:
    st.error(f"Falha ao conectar no Supabase: {e}")
    st.stop()

if pedidos.empty:
    st.info("Nenhum pedido no banco ainda. O monitor.py precisa rodar pelo menos 1 ciclo.")
    st.stop()

# ----------------------------------------------------------------------
# Filtros (sidebar)
# ----------------------------------------------------------------------

st.sidebar.header("Filtros")

contas_disponiveis = sorted(pedidos["conta_tiny"].dropna().unique())
canais_disponiveis = sorted(pedidos["canal"].dropna().unique())

contas_selecionadas = st.sidebar.multiselect("Conta Tiny", contas_disponiveis, default=contas_disponiveis)
canais_selecionados = st.sidebar.multiselect("Canal (marketplace/conta)", canais_disponiveis, default=canais_disponiveis)
so_negativos = st.sidebar.checkbox("So pedidos com margem negativa", value=False)
so_custo_ausente = st.sidebar.checkbox("So com custo ausente", value=False)

pedidos_f = pedidos[pedidos["conta_tiny"].isin(contas_selecionadas) & pedidos["canal"].isin(canais_selecionados)]
if so_negativos:
    pedidos_f = pedidos_f[pedidos_f["margem_contribuicao"] < 0]
if so_custo_ausente:
    pedidos_f = pedidos_f[pedidos_f["custo_ausente"] == True]  # noqa: E712

itens_f = itens[itens["conta_tiny"].isin(contas_selecionadas) & itens["canal"].isin(canais_selecionados)]

# ----------------------------------------------------------------------
# Indicadores gerais
# ----------------------------------------------------------------------

col1, col2, col3, col4 = st.columns(4)
col1.metric("Pedidos", f"{len(pedidos_f):,}".replace(",", "."))
col2.metric("Receita total", f"R$ {pedidos_f['receita'].sum():,.2f}".replace(",", "."))
col3.metric("Margem total", f"R$ {pedidos_f['margem_contribuicao'].sum():,.2f}".replace(",", "."))
margem_pct_media = (pedidos_f["margem_contribuicao"].sum() / pedidos_f["receita"].sum() * 100) if pedidos_f["receita"].sum() else 0
col4.metric("Margem % media (ponderada)", f"{margem_pct_media:.1f}%")

col5, col6 = st.columns(2)
col5.metric("Pedidos com margem negativa", int((pedidos_f["margem_contribuicao"] < 0).sum()))
col6.metric("Pedidos com custo ausente", int((pedidos_f["custo_ausente"] == True).sum()))  # noqa: E712

st.divider()

# ----------------------------------------------------------------------
# Por marketplace/canal
# ----------------------------------------------------------------------

st.subheader("Margem por canal (marketplace/conta)")
por_canal = (
    pedidos_f.groupby("canal")
    .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
    .reset_index()
)
por_canal["margem_pct"] = (por_canal["margem"] / por_canal["receita"] * 100).round(1)
por_canal = por_canal.sort_values("margem", ascending=False)

col_a, col_b = st.columns([2, 1])
col_a.bar_chart(por_canal.set_index("canal")["margem"])
col_b.dataframe(por_canal, hide_index=True, use_container_width=True)

# ----------------------------------------------------------------------
# Por conta Tiny
# ----------------------------------------------------------------------

st.subheader("Margem por conta Tiny (CNPJ)")
por_conta = (
    pedidos_f.groupby("conta_tiny")
    .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
    .reset_index()
)
por_conta["margem_pct"] = (por_conta["margem"] / por_conta["receita"] * 100).round(1)
por_conta = por_conta.sort_values("margem", ascending=False)

col_c, col_d = st.columns([2, 1])
col_c.bar_chart(por_conta.set_index("conta_tiny")["margem"])
col_d.dataframe(por_conta, hide_index=True, use_container_width=True)

# ----------------------------------------------------------------------
# Por produto (SKU)
# ----------------------------------------------------------------------

st.subheader("Margem por produto (SKU)")
if itens_f.empty:
    st.info("Sem dados de item ainda (so pedidos processados depois da mudanca pra rateio por item aparecem aqui).")
else:
    por_sku = (
        itens_f.groupby("sku")
        .agg(
            receita=("receita", "sum"),
            margem=("margem_contribuicao", "sum"),
            quantidade=("quantidade", "sum"),
            pedidos=("numero_pedido", "nunique"),
        )
        .reset_index()
    )
    por_sku["margem_pct"] = (por_sku["margem"] / por_sku["receita"] * 100).round(1)

    tab_piores, tab_melhores, tab_todos = st.tabs(["Piores margens", "Melhores margens", "Todos os SKUs"])
    with tab_piores:
        st.dataframe(por_sku.sort_values("margem").head(30), hide_index=True, use_container_width=True)
    with tab_melhores:
        st.dataframe(por_sku.sort_values("margem", ascending=False).head(30), hide_index=True, use_container_width=True)
    with tab_todos:
        st.dataframe(por_sku.sort_values("margem"), hide_index=True, use_container_width=True)

st.divider()

# ----------------------------------------------------------------------
# Venda a venda
# ----------------------------------------------------------------------

st.subheader("Venda a venda")
colunas_exibir = [
    "numero_pedido", "conta_tiny", "canal", "data_pedido", "receita",
    "margem_contribuicao", "margem_pct", "custo_ausente", "alertado",
]
coluna_ordenacao = "processado_em" if "processado_em" in pedidos_f.columns else "numero_pedido"
tabela_vendas = pedidos_f.sort_values(coluna_ordenacao, ascending=False)[colunas_exibir]
st.dataframe(tabela_vendas, hide_index=True, use_container_width=True)
