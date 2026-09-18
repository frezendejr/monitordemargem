"""Dashboard de margem de contribuicao - le do Supabase (tabelas
margin_monitor_pedidos e margin_monitor_itens, escritas por monitor.py via
supabase_writer.py; margin_monitor_custos, escrita por este proprio
dashboard quando o time preenche custo manualmente).

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


def salvar_custos_no_supabase(registros: list[dict]) -> None:
    """Upsert em margin_monitor_custos - usa a mesma chave anon do
    dashboard (a tabela permite escrita por essa chave, ver
    supabase_schema_custos.sql). monitor.py le isso no proximo ciclo."""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_custos?on_conflict=sku",
        json=registros,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=15,
    )
    resp.raise_for_status()


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

pedidos["data_pedido_dt"] = pd.to_datetime(pedidos["data_pedido"], format="%d/%m/%Y", errors="coerce")
itens["data_pedido_dt"] = pd.to_datetime(itens["data_pedido"], format="%d/%m/%Y", errors="coerce")

# ----------------------------------------------------------------------
# Filtros (sidebar)
# ----------------------------------------------------------------------

st.sidebar.header("Filtros")

contas_disponiveis = sorted(pedidos["conta_tiny"].dropna().unique())
canais_disponiveis = sorted(pedidos["canal"].dropna().unique())

contas_selecionadas = st.sidebar.multiselect("Conta Tiny", contas_disponiveis, default=contas_disponiveis)
canais_selecionados = st.sidebar.multiselect("Canal (marketplace/conta)", canais_disponiveis, default=canais_disponiveis)

datas_validas = pedidos["data_pedido_dt"].dropna()
if not datas_validas.empty:
    data_min, data_max = datas_validas.min().date(), datas_validas.max().date()
    periodo = st.sidebar.date_input(
        "Periodo (data do pedido)", value=(data_min, data_max), min_value=data_min, max_value=data_max
    )
    if isinstance(periodo, tuple) and len(periodo) == 2:
        data_inicio, data_fim = periodo
    else:
        data_inicio, data_fim = data_min, data_max
else:
    data_inicio, data_fim = None, None

so_negativos = st.sidebar.checkbox("So pedidos com margem negativa", value=False)
so_custo_ausente = st.sidebar.checkbox("So com custo ausente", value=False)

pedidos_f = pedidos[pedidos["conta_tiny"].isin(contas_selecionadas) & pedidos["canal"].isin(canais_selecionados)]
itens_f = itens[itens["conta_tiny"].isin(contas_selecionadas) & itens["canal"].isin(canais_selecionados)]

if data_inicio and data_fim:
    pedidos_f = pedidos_f[
        (pedidos_f["data_pedido_dt"].dt.date >= data_inicio) & (pedidos_f["data_pedido_dt"].dt.date <= data_fim)
    ]
    itens_f = itens_f[(itens_f["data_pedido_dt"].dt.date >= data_inicio) & (itens_f["data_pedido_dt"].dt.date <= data_fim)]

if so_negativos:
    pedidos_f = pedidos_f[pedidos_f["margem_contribuicao"] < 0]
if so_custo_ausente:
    pedidos_f = pedidos_f[pedidos_f["custo_ausente"] == True]  # noqa: E712

# ----------------------------------------------------------------------
# Indicadores gerais
# ----------------------------------------------------------------------

col1, col2, col3, col4 = st.columns(4)
col1.metric("Pedidos", f"{len(pedidos_f):,}".replace(",", "."))
col2.metric("Receita total", f"R$ {pedidos_f['receita'].sum():,.2f}".replace(",", "."))
col3.metric("Margem total", f"R$ {pedidos_f['margem_contribuicao'].sum():,.2f}".replace(",", "."))

# So considera itens com custo conhecido - custo_ausente=True usa CMV=0, o
# que deixaria essa media artificialmente otimista se entrasse na conta.
itens_com_custo = itens_f[itens_f["custo_ausente"] == False]  # noqa: E712
receita_com_custo = itens_com_custo["receita"].sum()
margem_com_custo = itens_com_custo["margem_contribuicao"].sum()
margem_pct_media = (margem_com_custo / receita_com_custo * 100) if receita_com_custo else 0
col4.metric("Margem % media (so c/ custo)", f"{margem_pct_media:.1f}%")

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
# Produtos sem custo
# ----------------------------------------------------------------------

st.subheader("Produtos sem custo cadastrado")
sem_custo = itens_f[itens_f["custo_ausente"] == True]  # noqa: E712

if sem_custo.empty:
    st.success("Nenhum produto sem custo no filtro atual.")
else:
    resumo_sem_custo = (
        sem_custo.groupby("sku")
        .agg(quantidade=("quantidade", "sum"), receita_afetada=("receita", "sum"), pedidos=("numero_pedido", "nunique"))
        .reset_index()
        .sort_values("receita_afetada", ascending=False)
    )
    st.write(
        f"{len(resumo_sem_custo)} SKU(s) sem custo, afetando "
        f"R$ {resumo_sem_custo['receita_afetada'].sum():,.2f} de receita no filtro atual.".replace(",", ".")
    )

    csv = resumo_sem_custo.to_csv(index=False).encode("utf-8")
    st.download_button("Baixar lista (CSV) para preencher e importar", csv, "produtos_sem_custo.csv", "text/csv")

    st.caption(
        "Ou preenche o custo direto aqui embaixo (coluna 'Custo (R$)') e clica em Salvar - "
        "grava no Supabase e o monitor.py passa a usar esse custo a partir do proximo ciclo."
    )
    resumo_sem_custo["custo_novo"] = None
    editado = st.data_editor(
        resumo_sem_custo[["sku", "quantidade", "receita_afetada", "pedidos", "custo_novo"]],
        column_config={"custo_novo": st.column_config.NumberColumn("Custo (R$)", min_value=0.0, step=0.01)},
        disabled=["sku", "quantidade", "receita_afetada", "pedidos"],
        hide_index=True,
        use_container_width=True,
        key="editor_custos",
    )

    if st.button("Salvar custos preenchidos"):
        preenchidos = editado[editado["custo_novo"].notna() & (editado["custo_novo"] > 0)]
        if preenchidos.empty:
            st.warning("Nenhum custo preenchido pra salvar.")
        else:
            registros = preenchidos[["sku", "custo_novo"]].rename(columns={"custo_novo": "custo"}).to_dict("records")
            try:
                salvar_custos_no_supabase(registros)
                st.success(f"{len(registros)} custo(s) salvo(s) no Supabase.")
            except Exception as e:
                st.error(f"Falha ao salvar: {e}")

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
