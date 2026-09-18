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
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Margem Grupo Amo", page_icon="📊", layout="wide")


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


def _fmt_moeda(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


st.title("📊 Margem de contribuição — Grupo Amo")

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

datas_validas = pedidos["data_pedido_dt"].dropna()
data_min_dados = datas_validas.min().date() if not datas_validas.empty else date.today()
data_max_dados = datas_validas.max().date() if not datas_validas.empty else date.today()

hoje = date.today()
hoje_no_intervalo = min(max(hoje, data_min_dados), data_max_dados)
opcoes_periodo = {
    "Hoje": (hoje, hoje),
    "Ontem": (hoje - timedelta(days=1), hoje - timedelta(days=1)),
    "Últimos 7 dias": (hoje - timedelta(days=6), hoje),
    "Últimos 30 dias": (hoje - timedelta(days=29), hoje),
    "Este mês": (hoje.replace(day=1), hoje),
    "Tudo": (data_min_dados, data_max_dados),
    "Data específica": None,
    "Intervalo personalizado": None,
}
escolha_periodo = st.sidebar.selectbox("Período", list(opcoes_periodo.keys()), index=0)

if escolha_periodo == "Data específica":
    dia_escolhido = st.sidebar.date_input(
        "Qual dia?", value=hoje_no_intervalo, min_value=data_min_dados, max_value=data_max_dados
    )
    data_inicio = data_fim = dia_escolhido
elif escolha_periodo == "Intervalo personalizado":
    periodo = st.sidebar.date_input(
        "Escolha o intervalo",
        value=(data_min_dados, data_max_dados),
        min_value=data_min_dados,
        max_value=data_max_dados,
    )
    if isinstance(periodo, tuple) and len(periodo) == 2:
        data_inicio, data_fim = periodo
    else:
        data_inicio, data_fim = data_min_dados, data_max_dados
else:
    data_inicio, data_fim = opcoes_periodo[escolha_periodo]

contas_disponiveis = sorted(pedidos["conta_tiny"].dropna().unique())
canais_disponiveis = sorted(pedidos["canal"].dropna().unique())
contas_selecionadas = st.sidebar.multiselect("Conta Tiny", contas_disponiveis, default=contas_disponiveis)
canais_selecionados = st.sidebar.multiselect("Canal (marketplace/conta)", canais_disponiveis, default=canais_disponiveis)

so_negativos = st.sidebar.checkbox("Só pedidos com margem negativa", value=False)
so_custo_ausente = st.sidebar.checkbox("Só com custo ausente", value=False)

pedidos_f = pedidos[pedidos["conta_tiny"].isin(contas_selecionadas) & pedidos["canal"].isin(canais_selecionados)]
itens_f = itens[itens["conta_tiny"].isin(contas_selecionadas) & itens["canal"].isin(canais_selecionados)]

pedidos_f = pedidos_f[
    (pedidos_f["data_pedido_dt"].dt.date >= data_inicio) & (pedidos_f["data_pedido_dt"].dt.date <= data_fim)
]
itens_f = itens_f[(itens_f["data_pedido_dt"].dt.date >= data_inicio) & (itens_f["data_pedido_dt"].dt.date <= data_fim)]

if so_negativos:
    pedidos_f = pedidos_f[pedidos_f["margem_contribuicao"] < 0]
if so_custo_ausente:
    pedidos_f = pedidos_f[pedidos_f["custo_ausente"] == True]  # noqa: E712

rotulo_periodo = (
    data_inicio.strftime("%d/%m/%Y")
    if data_inicio == data_fim
    else f"{data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}"
)
st.caption(f"Período: **{rotulo_periodo}** · {len(pedidos_f)} pedido(s)")

if pedidos_f.empty:
    st.info(f"Nenhum pedido no período selecionado ({rotulo_periodo}). Tente outro período no filtro ao lado.")
    st.stop()

# ----------------------------------------------------------------------
# Abas
# ----------------------------------------------------------------------

aba_visao, aba_produtos, aba_custos, aba_vendas = st.tabs(
    ["📈 Visão geral", "📦 Produtos", "⚠️ Custos pendentes", "🧾 Vendas"]
)

# ---- Visão geral -------------------------------------------------------
with aba_visao:
    with st.container(border=True):
        c1, c2 = st.columns(2)
        c1.metric("Pedidos", f"{len(pedidos_f):,}".replace(",", "."))
        c2.metric("Receita total", _fmt_moeda(pedidos_f["receita"].sum()))

        c3, c4 = st.columns(2)
        c3.metric("Margem total", _fmt_moeda(pedidos_f["margem_contribuicao"].sum()))

        itens_com_custo = itens_f[itens_f["custo_ausente"] == False]  # noqa: E712
        receita_com_custo = itens_com_custo["receita"].sum()
        margem_com_custo = itens_com_custo["margem_contribuicao"].sum()
        margem_pct_media = (margem_com_custo / receita_com_custo * 100) if receita_com_custo else 0
        c4.metric("Margem % (só c/ custo)", f"{margem_pct_media:.1f}%")

        c5, c6 = st.columns(2)
        c5.metric("Pedidos com margem negativa", int((pedidos_f["margem_contribuicao"] < 0).sum()))
        c6.metric("Pedidos com custo ausente", int((pedidos_f["custo_ausente"] == True).sum()))  # noqa: E712

    st.subheader("Margem por canal")
    por_canal = (
        pedidos_f.groupby("canal")
        .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
        .reset_index()
    )
    por_canal["margem_pct"] = (por_canal["margem"] / por_canal["receita"] * 100).round(1)
    por_canal = por_canal.sort_values("margem", ascending=False)
    st.bar_chart(por_canal.set_index("canal")["margem"])
    st.dataframe(por_canal, hide_index=True, use_container_width=True)

    st.subheader("Margem por conta Tiny (CNPJ)")
    por_conta = (
        pedidos_f.groupby("conta_tiny")
        .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
        .reset_index()
    )
    por_conta["margem_pct"] = (por_conta["margem"] / por_conta["receita"] * 100).round(1)
    por_conta = por_conta.sort_values("margem", ascending=False)
    st.bar_chart(por_conta.set_index("conta_tiny")["margem"])
    st.dataframe(por_conta, hide_index=True, use_container_width=True)

# ---- Produtos -----------------------------------------------------------
with aba_produtos:
    if itens_f.empty:
        st.info("Sem dados de item no período selecionado.")
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
            st.dataframe(
                por_sku.sort_values("margem", ascending=False).head(30), hide_index=True, use_container_width=True
            )
        with tab_todos:
            st.dataframe(por_sku.sort_values("margem"), hide_index=True, use_container_width=True)

# ---- Custos pendentes ----------------------------------------------------
with aba_custos:
    sem_custo = itens_f[itens_f["custo_ausente"] == True]  # noqa: E712

    if sem_custo.empty:
        st.success("Nenhum produto sem custo no filtro atual.")
    else:
        resumo_sem_custo = (
            sem_custo.groupby("sku")
            .agg(
                quantidade=("quantidade", "sum"), receita_afetada=("receita", "sum"), pedidos=("numero_pedido", "nunique")
            )
            .reset_index()
            .sort_values("receita_afetada", ascending=False)
        )
        st.write(
            f"**{len(resumo_sem_custo)} SKU(s)** sem custo, afetando "
            f"**{_fmt_moeda(resumo_sem_custo['receita_afetada'].sum())}** de receita no período."
        )

        csv = resumo_sem_custo.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Baixar lista (CSV) para preencher e importar", csv, "produtos_sem_custo.csv", "text/csv")

        st.caption(
            "Ou preenche o custo direto na coluna 'Custo (R$)' abaixo e clica em Salvar — "
            "grava no Supabase e o monitor.py passa a usar esse custo a partir do próximo ciclo."
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

        if st.button("💾 Salvar custos preenchidos"):
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

# ---- Venda a venda --------------------------------------------------------
with aba_vendas:
    colunas_exibir = [
        "numero_pedido", "conta_tiny", "canal", "data_pedido", "receita",
        "margem_contribuicao", "margem_pct", "custo_ausente", "alertado",
    ]
    coluna_ordenacao = "processado_em" if "processado_em" in pedidos_f.columns else "numero_pedido"
    tabela_vendas = pedidos_f.sort_values(coluna_ordenacao, ascending=False)[colunas_exibir]
    st.dataframe(tabela_vendas, hide_index=True, use_container_width=True)
