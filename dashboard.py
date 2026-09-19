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

import io
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


def _codigo_pai(sku: str) -> str:
    """Convencao Amo Shoes: SKU = codigo_pai (N digitos) + tamanho (2
    digitos) - confirmado contra dado real (SKU 916039 = "Tamanho: 39" no
    proprio extrato do Mercado Livre, e o grupo 902834-902839 = pai 9028).
    SKU sem esse padrao (curto demais ou nao-numerico) e seu proprio grupo."""
    sku = str(sku).strip()
    if len(sku) > 2 and sku[:-2].isdigit() and sku[-2:].isdigit():
        return sku[:-2]
    return sku


def expandir_por_codigo_pai(registros: list[dict]) -> list[dict]:
    """Custo nao muda entre tamanhos do mesmo produto - preencher o custo de
    UM SKU aplica automaticamente o mesmo custo a todos os SKUs irmaos (mesmo
    codigo_pai) que ja apareceram em alguma venda, mesmo que nao estejam na
    lista de pendentes agora (podem ja ter outro custo, possivelmente errado,
    ou nunca terem sido marcados como pendentes)."""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    expandido = {str(r["sku"]): r["custo"] for r in registros}
    for registro in registros:
        pai = _codigo_pai(registro["sku"])
        resp = requests.get(
            f"{url}/rest/v1/margin_monitor_itens",
            params={"select": "sku", "sku": f"like.{pai}*"},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        for linha in resp.json():
            sku_irmao = linha["sku"]
            if _codigo_pai(sku_irmao) == pai:
                expandido.setdefault(sku_irmao, registro["custo"])

    return [{"sku": sku, "custo": custo} for sku, custo in expandido.items()]


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


# ----------------------------------------------------------------------
# Metas (faturamento/margem do mes, divididas por dia via sazonalidade)
# ----------------------------------------------------------------------

def carregar_metas() -> dict:
    """{mes (date, dia 1): {"meta_faturamento":..., "meta_margem_pct":...}}"""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_metas",
        params={"select": "mes,meta_faturamento,meta_margem_pct"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return {pd.to_datetime(r["mes"]).date(): r for r in resp.json()}


def salvar_meta(mes: date, meta_faturamento: float, meta_margem_pct: float) -> None:
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_metas?on_conflict=mes",
        json=[
            {
                "mes": mes.isoformat(),
                "meta_faturamento": meta_faturamento,
                "meta_margem_pct": meta_margem_pct,
                "atualizado_em": pd.Timestamp.utcnow().isoformat(),
            }
        ],
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        },
        timeout=15,
    )
    resp.raise_for_status()


def calcular_indices_dia_semana(pedidos_hist: pd.DataFrame, janela_dias: int = 90) -> dict[int, float]:
    """Sazonalidade por dia da semana (0=segunda...6=domingo): media de
    receita de cada dia da semana nos ultimos `janela_dias`, normalizada pela
    media geral. Indice 1.0 = dia tipico; 1.3 = 30% acima da media. Mesmo
    metodo da planilha de metas de referencia ("Índices por dia da semana
    calculados sobre os dias com dado verificado")."""
    limite = pd.Timestamp(date.today() - timedelta(days=janela_dias))
    hist = pedidos_hist[pedidos_hist["data_pedido_dt"] >= limite]
    if hist.empty:
        return {i: 1.0 for i in range(7)}

    por_data = hist.groupby(hist["data_pedido_dt"].dt.date)["receita"].sum()
    por_data.index = pd.to_datetime(por_data.index)
    media_por_dia_semana = por_data.groupby(por_data.index.dayofweek).mean()
    media_geral = media_por_dia_semana.mean()
    if not media_geral:
        return {i: 1.0 for i in range(7)}

    indices = (media_por_dia_semana / media_geral).to_dict()
    return {i: indices.get(i, 1.0) for i in range(7)}


def _dias_do_mes(mes: date) -> list[date]:
    proximo = date(mes.year + 1, 1, 1) if mes.month == 12 else date(mes.year, mes.month + 1, 1)
    dias, d = [], mes
    while d < proximo:
        dias.append(d)
        d += timedelta(days=1)
    return dias


def _meses_no_intervalo(data_inicio: date, data_fim: date) -> list[date]:
    meses, atual = [], data_inicio.replace(day=1)
    while atual <= data_fim:
        meses.append(atual)
        atual = date(atual.year + 1, 1, 1) if atual.month == 12 else date(atual.year, atual.month + 1, 1)
    return meses


def calcular_meta_periodo(
    data_inicio: date, data_fim: date, metas_por_mes: dict, indices: dict[int, float]
) -> tuple[float, float, bool]:
    """Reparte a meta MENSAL pelos dias do periodo pedido, proporcional ao
    indice de sazonalidade de cada dia da semana. Retorna
    (meta_faturamento, meta_margem_r$, tem_mes_sem_meta_cadastrada)."""
    total_fat = 0.0
    total_margem = 0.0
    faltando = False
    for mes in _meses_no_intervalo(data_inicio, data_fim):
        meta_mes = metas_por_mes.get(mes)
        if not meta_mes:
            faltando = True
            continue
        dias_mes = _dias_do_mes(mes)
        soma_pesos_mes = sum(indices[d.weekday()] for d in dias_mes)
        if not soma_pesos_mes:
            continue
        for d in dias_mes:
            if data_inicio <= d <= data_fim:
                peso_dia = indices[d.weekday()] / soma_pesos_mes
                meta_dia_fat = meta_mes["meta_faturamento"] * peso_dia
                total_fat += meta_dia_fat
                total_margem += meta_dia_fat * meta_mes["meta_margem_pct"] / 100
    return round(total_fat, 2), round(total_margem, 2), faltando


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
    # -- Meta do mes: cadastro + comparativo com o realizado --------------
    try:
        metas_por_mes = carregar_metas()
    except Exception:
        metas_por_mes = {}

    with st.expander("🎯 Meta do mês (faturamento + margem)"):
        mes_config = st.date_input(
            "Configurar meta de qual mês?", value=date.today().replace(day=1), format="DD/MM/YYYY"
        ).replace(day=1)
        meta_existente = metas_por_mes.get(mes_config, {})
        col_a, col_b = st.columns(2)
        novo_fat = col_a.number_input(
            "Meta de faturamento do mês (R$)",
            min_value=0.0,
            step=1000.0,
            value=float(meta_existente.get("meta_faturamento", 0.0)),
        )
        novo_pct = col_b.number_input(
            "Meta de margem (%)",
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            value=float(meta_existente.get("meta_margem_pct", 15.0)),
        )
        st.caption(
            "A meta é sempre do MÊS inteiro - a divisão por dia é automática, "
            "proporcional a como cada dia da semana costuma vender (últimos 90 dias)."
        )
        if st.button("💾 Salvar meta"):
            try:
                salvar_meta(mes_config, novo_fat, novo_pct)
                st.success(f"Meta de {mes_config.strftime('%m/%Y')} salva.")
                st.rerun()
            except Exception as e:
                st.error(f"Falha ao salvar: {e}")

    indices_dia_semana = calcular_indices_dia_semana(pedidos)
    meta_fat_periodo, meta_margem_periodo, meta_incompleta = calcular_meta_periodo(
        data_inicio, data_fim, metas_por_mes, indices_dia_semana
    )
    # Faturamento/margem real do periodo SEMPRE de todas as contas/canais
    # (a meta e um numero unico da empresa toda - nao faz sentido comparar
    # com um subconjunto filtrado na sidebar).
    pedidos_periodo_empresa = pedidos[
        (pedidos["data_pedido_dt"].dt.date >= data_inicio) & (pedidos["data_pedido_dt"].dt.date <= data_fim)
    ]
    faturamento_real = pedidos_periodo_empresa["receita"].sum()
    margem_real = pedidos_periodo_empresa["margem_contribuicao"].sum()

    itens_com_custo = itens_f[itens_f["custo_ausente"] == False]  # noqa: E712
    receita_com_custo = itens_com_custo["receita"].sum()
    margem_com_custo = itens_com_custo["margem_contribuicao"].sum()
    margem_pct_media = (margem_com_custo / receita_com_custo * 100) if receita_com_custo else 0

    with st.container(border=True):
        st.caption("Faturamento e margem (com meta) SEMPRE somam todas as contas/canais, independente do filtro ao lado.")
        cm1, cm2 = st.columns(2)
        cm1.metric("Faturamento", _fmt_moeda(faturamento_real))
        cm2.metric(
            "Meta de Faturamento",
            _fmt_moeda(meta_fat_periodo) if not meta_incompleta else "sem meta",
            delta=(f"{(faturamento_real / meta_fat_periodo * 100 - 100):+.1f}%" if meta_fat_periodo else None),
        )
        cm3, cm4 = st.columns(2)
        cm3.metric("Margem", _fmt_moeda(margem_real))
        cm4.metric(
            "Meta de Margem",
            _fmt_moeda(meta_margem_periodo) if not meta_incompleta else "sem meta",
            delta=(f"{(margem_real / meta_margem_periodo * 100 - 100):+.1f}%" if meta_margem_periodo else None),
        )
        if meta_incompleta:
            st.caption("⚠️ Algum mês do período selecionado ainda não tem meta cadastrada acima.")

        st.divider()

        c1, c2 = st.columns(2)
        c1.metric("Pedidos", f"{len(pedidos_f):,}".replace(",", "."))
        c2.metric("Receita total", _fmt_moeda(pedidos_f["receita"].sum()))

        c3, c4 = st.columns(2)
        c3.metric("Margem total", _fmt_moeda(pedidos_f["margem_contribuicao"].sum()))
        c4.metric("Margem % (só c/ custo)", f"{margem_pct_media:.1f}%")

        c5, c6 = st.columns(2)
        c5.metric("Pedidos com margem negativa", int((pedidos_f["margem_contribuicao"] < 0).sum()))
        c6.metric("Pedidos com custo ausente", int((pedidos_f["custo_ausente"] == True).sum()))  # noqa: E712

    # -- Vendas abaixo do custo (alerta) -----------------------------------
    abaixo_custo = itens_f[
        (itens_f["custo_ausente"] == False)  # noqa: E712
        & (itens_f["cmv"] > 0)
        & (itens_f["receita"] < itens_f["cmv"])
    ]
    n_abaixo_custo = len(abaixo_custo)

    if n_abaixo_custo > 0:
        st.markdown(
            """
            <style>
            @keyframes piscar_abaixo_custo { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
            .alerta-abaixo-custo {
                animation: piscar_abaixo_custo 1.1s infinite;
                background-color: #d32f2f; color: white; padding: 14px;
                border-radius: 8px; font-weight: bold; text-align: center;
                margin-bottom: 8px;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="alerta-abaixo-custo">🚨 {n_abaixo_custo} venda(s) abaixo do CUSTO da mercadoria '
            f'no período (nem o CMV a receita cobre) - clique abaixo pra ver quais</div>',
            unsafe_allow_html=True,
        )
        if st.button(f"🔎 Ver os {n_abaixo_custo} caso(s) de venda abaixo do custo"):
            st.session_state["mostrar_abaixo_custo"] = not st.session_state.get("mostrar_abaixo_custo", False)

        if st.session_state.get("mostrar_abaixo_custo"):
            # Referencia de custo "correto" do codigo pai: a moda do custo
            # unitario entre TODOS os irmaos (nao so o periodo filtrado) -
            # se essa venda diverge dessa referencia, o problema e o CUSTO
            # cadastrado (provavel erro de digitacao); se bate com a
            # referencia, o custo esta certo e o problema e o PRECO de venda.
            itens_ref = itens.copy()
            itens_ref["codigo_pai"] = itens_ref["sku"].map(_codigo_pai)
            itens_ref["custo_unitario"] = itens_ref["cmv"] / itens_ref["quantidade"].replace(0, pd.NA)
            custo_referencia = itens_ref.groupby("codigo_pai")["custo_unitario"].agg(
                lambda s: s.mode().iloc[0] if not s.mode().empty else s.median()
            )

            detalhe = abaixo_custo.copy()
            detalhe["codigo_pai"] = detalhe["sku"].map(_codigo_pai)
            detalhe["custo_unitario"] = detalhe["cmv"] / detalhe["quantidade"].replace(0, pd.NA)
            detalhe["custo_referencia"] = detalhe["codigo_pai"].map(custo_referencia)
            detalhe["perda"] = detalhe["cmv"] - detalhe["receita"]
            if "anuncio_id" not in detalhe.columns:
                detalhe["anuncio_id"] = None

            def _sugestao(row) -> str:
                if pd.notna(row["custo_referencia"]) and abs(row["custo_unitario"] - row["custo_referencia"]) > 0.01:
                    return (
                        f"⚠️ Revisar CUSTO cadastrado - diverge dos irmãos "
                        f"(R$ {row['custo_unitario']:.2f} vs R$ {row['custo_referencia']:.2f} do grupo)"
                    )
                return "💲 Revisar PREÇO de venda ou pausar anúncio - custo cadastrado bate com os irmãos"

            detalhe["sugestão"] = detalhe.apply(_sugestao, axis=1)

            st.dataframe(
                detalhe[
                    [
                        "numero_pedido", "canal", "codigo_pai", "sku", "anuncio_id",
                        "quantidade", "receita", "cmv", "perda", "sugestão",
                    ]
                ].sort_values("perda", ascending=False),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "numero_pedido": "Pedido",
                    "canal": "Marketplace/conta",
                    "codigo_pai": "Código pai",
                    "anuncio_id": "Anúncio (ML)",
                    "receita": st.column_config.NumberColumn("Receita", format="R$ %.2f"),
                    "cmv": st.column_config.NumberColumn("CMV", format="R$ %.2f"),
                    "perda": st.column_config.NumberColumn("Perda (CMV − receita)", format="R$ %.2f"),
                },
            )
            st.caption(
                "Anúncio (ML) só aparece pra vendas do Mercado Livre processadas depois desse recurso existir - "
                "em branco = ainda não capturado (não tem como o dashboard buscar isso ao vivo)."
            )
    else:
        st.success("✅ Nenhuma venda abaixo do custo da mercadoria no período.")

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

        buffer_xlsx = io.BytesIO()
        resumo_sem_custo.assign(custo=None).to_excel(buffer_xlsx, index=False, sheet_name="custos_pendentes")
        st.download_button(
            "⬇️ Baixar lista (Excel) para preencher e importar",
            buffer_xlsx.getvalue(),
            "produtos_sem_custo.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.caption(
            "Preenche a coluna 'custo' e importa de volta abaixo, ou edita direto na tabela mais "
            "embaixo. Preencher o custo de UM tamanho aplica automaticamente o mesmo custo a "
            "todos os tamanhos do mesmo código pai (custo não muda por tamanho). Nos dois casos "
            "grava no Supabase na hora. Pedidos NOVOS já usam o custo certo no ciclo seguinte; "
            "pedidos JÁ LANÇADOS com esse SKU (que aparecem como 'custo ausente' hoje) são "
            "corrigidos automaticamente pelo monitor.py em até ~15 min (ele não pode escrever "
            "aqui direto - só o dashboard - por segurança, já que esse link é compartilhado com "
            "o time)."
        )

        arquivo = st.file_uploader("Importar planilha preenchida", type=["xlsx", "csv"], key="upload_custos")
        if arquivo is not None:
            tabela_importada = (
                pd.read_excel(arquivo) if arquivo.name.endswith(".xlsx") else pd.read_csv(arquivo)
            )
            faltando = {"sku", "custo"} - set(tabela_importada.columns)
            if faltando:
                st.error(f"Planilha sem a(s) coluna(s): {', '.join(faltando)}")
            else:
                validos = tabela_importada[
                    tabela_importada["custo"].notna() & (pd.to_numeric(tabela_importada["custo"], errors="coerce") > 0)
                ]
                if validos.empty:
                    st.warning("Nenhuma linha com custo preenchido nessa planilha.")
                elif st.button(f"💾 Importar {len(validos)} custo(s)"):
                    registros = validos[["sku", "custo"]].astype({"sku": str}).to_dict("records")
                    try:
                        registros = expandir_por_codigo_pai(registros)
                        salvar_custos_no_supabase(registros)
                        st.success(f"{len(registros)} custo(s) salvo(s) no Supabase (incluindo tamanhos irmãos).")
                        carregar_tabela.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Falha ao salvar: {e}")

        resumo_sem_custo["codigo_pai"] = resumo_sem_custo["sku"].map(_codigo_pai)
        resumo_sem_custo["custo_novo"] = None
        editado = st.data_editor(
            resumo_sem_custo[["sku", "codigo_pai", "quantidade", "receita_afetada", "pedidos", "custo_novo"]],
            column_config={
                "codigo_pai": st.column_config.TextColumn("Código pai"),
                "custo_novo": st.column_config.NumberColumn("Custo (R$)", min_value=0.0, step=0.01),
            },
            disabled=["sku", "codigo_pai", "quantidade", "receita_afetada", "pedidos"],
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
                    registros = expandir_por_codigo_pai(registros)
                    salvar_custos_no_supabase(registros)
                    st.success(f"{len(registros)} custo(s) salvo(s) no Supabase (incluindo tamanhos irmãos).")
                    carregar_tabela.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Falha ao salvar: {e}")

# ---- Venda a venda --------------------------------------------------------
with aba_vendas:
    coluna_ordenacao = "processado_em" if "processado_em" in pedidos_f.columns else "numero_pedido"
    tabela_vendas = pedidos_f.sort_values(coluna_ordenacao, ascending=False).copy()

    # Pedidos gravados antes do detalhamento (ou ainda nao recalculados pelo
    # monitor) nao tem valor_venda/cmv/imposto/comissao/frete/ads - cai pra
    # receita/None em vez de sumir da tabela.
    for coluna in ["cmv", "imposto", "comissao", "frete", "ads"]:
        if coluna not in tabela_vendas.columns:
            tabela_vendas[coluna] = None
    if "valor_venda" not in tabela_vendas.columns:
        tabela_vendas["valor_venda"] = None
    tabela_vendas["valor_venda"] = tabela_vendas["valor_venda"].fillna(tabela_vendas["receita"])

    st.caption(
        "Valor Venda → (-) CMV → (-) Imposto → (-) Comissão → (-) Frete → (-) Ads → (=) Margem. "
        "Pro Mercado Livre, Comissão/Frete são o valor REAL cobrado (via API do ML), não estimativa - "
        "os outros canais usam % estimado (Tiny não expõe o valor exato cobrado pelo marketplace). "
        "Vazio = pedido lançado antes desse detalhamento existir."
    )

    colunas_exibir = [
        "numero_pedido", "conta_tiny", "canal", "data_pedido", "valor_venda", "cmv",
        "imposto", "comissao", "frete", "ads", "margem_contribuicao", "margem_pct",
        "custo_ausente", "alertado",
    ]
    st.dataframe(
        tabela_vendas[colunas_exibir],
        hide_index=True,
        use_container_width=True,
        column_config={
            "valor_venda": st.column_config.NumberColumn("Valor Venda", format="R$ %.2f"),
            "cmv": st.column_config.NumberColumn("CMV", format="R$ %.2f"),
            "imposto": st.column_config.NumberColumn("Imposto", format="R$ %.2f"),
            "comissao": st.column_config.NumberColumn("Comissão", format="R$ %.2f"),
            "frete": st.column_config.NumberColumn("Frete", format="R$ %.2f"),
            "ads": st.column_config.NumberColumn("Ads", format="R$ %.2f"),
            "margem_contribuicao": st.column_config.NumberColumn("Margem", format="R$ %.2f"),
            "margem_pct": st.column_config.NumberColumn("Margem %", format="%.1f%%"),
        },
    )
