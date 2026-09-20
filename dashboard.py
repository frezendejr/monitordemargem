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
from datetime import date, datetime, timedelta, timezone

import altair as alt
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


def _hoje_brasilia() -> date:
    """date.today() usa o fuso do SERVIDOR - o dashboard roda no Streamlit
    Community Cloud, cujo relogio e UTC. Como Brasilia esta 3h atras, isso
    fazia "hoje" virar o dia seguinte 3h ANTES da meia-noite real em
    Brasilia (bug real, 2026-09-19: usuario viu o filtro "Hoje" mostrando
    20/09 as 21:33 de 19/09 no horario dele - que e so 00:33 UTC do dia
    20, o servidor ja tinha virado o dia). Usar isso em vez de
    date.today() em TUDO que precisa saber "que dia e hoje" pro time."""
    return (datetime.now(timezone.utc) - timedelta(hours=3)).date()


_PAGINA_SUPABASE = 1000


@st.cache_data(ttl=120)
def carregar_tabela(nome: str) -> pd.DataFrame:
    """O Supabase tem um teto de 1000 linhas por resposta (db-max-rows) -
    ignora qualquer `limit` maior pedido na query. Passar so `limit=20000`
    sem paginar (bug real, achado em 2026-09-19: a tabela ja tinha milhares
    de pedidos por causa dos backfills, e o dashboard so via as 1000
    ULTIMAS gravadas - cortava praticamente todo o historico mais antigo).
    Pagina de verdade via Range ate a pagina vir incompleta."""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    linhas: list[dict] = []
    inicio = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/{nome}",
            headers={**headers, "Range": f"{inicio}-{inicio + _PAGINA_SUPABASE - 1}"},
            params={"select": "*", "order": "processado_em.desc"},
            timeout=30,
        )
        resp.raise_for_status()
        pagina = resp.json()
        linhas.extend(pagina)
        if len(pagina) < _PAGINA_SUPABASE:
            break
        inicio += _PAGINA_SUPABASE

    return pd.DataFrame(linhas)


SEM_CATEGORIA = "Sem categoria"


@st.cache_data(ttl=120)
def carregar_categorias_produto() -> dict[str, str]:
    """{codigo_pai: categoria} - margin_monitor_categorias e escrita so
    pelo backend (recalcular_categoria_pendente, via API do Meli), o
    dashboard so le. Nao usa carregar_tabela porque essa tabela nao tem
    coluna processado_em (usa atualizado_em)."""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    categorias: dict[str, str] = {}
    inicio = 0
    while True:
        resp = requests.get(
            f"{url}/rest/v1/margin_monitor_categorias",
            headers={**headers, "Range": f"{inicio}-{inicio + _PAGINA_SUPABASE - 1}"},
            params={"select": "codigo_pai,categoria"},
            timeout=30,
        )
        resp.raise_for_status()
        pagina = resp.json()
        categorias.update({r["codigo_pai"]: r["categoria"] for r in pagina})
        if len(pagina) < _PAGINA_SUPABASE:
            break
        inicio += _PAGINA_SUPABASE

    return categorias


def _codigo_pai(sku: str) -> str:
    """Convencao Amo Shoes: SKU = codigo_pai (N digitos) + tamanho (2
    digitos) - confirmado contra dado real (SKU 916039 = "Tamanho: 39" no
    proprio extrato do Mercado Livre, e o grupo 902834-902839 = pai 9028).
    SKU sem esse padrao (curto demais ou nao-numerico) e seu proprio grupo."""
    sku = str(sku).strip()
    if len(sku) > 2 and sku[:-2].isdigit() and sku[-2:].isdigit():
        return sku[:-2]
    return sku


def _marketplace_generico(canal: str) -> str:
    """Agrupa os canais (meli_conta_1..4, shopee_1/2, etc.) pelo MARKETPLACE
    em si - pra graficos onde nao interessa qual conta/CNPJ especifico
    vendeu, so o marketplace (ex.: "Meli" junta as 4 contas do Mercado
    Livre num so fatia)."""
    canal = str(canal)
    if canal.startswith("meli_conta"):
        return "Mercado Livre"
    if canal.startswith("shopee"):
        return "Shopee"
    if canal == "tiktok_shop":
        return "TikTok Shop"
    return canal.capitalize()


def _nome_conta(canal: str) -> str:
    """Nome legivel da conta/canal bruto pra tabela detalhada (ex.:
    "meli_conta_1" -> "Meli Conta 1")."""
    return str(canal).replace("_", " ").title()


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


_CORES_MARKETPLACE = {
    "Mercado Livre": "#FFE600",  # amarelo da marca
    "Shopee": "#EE4D2D",         # laranja da marca
    "Amazon": "#146EB4",         # azul da marca
    "TikTok Shop": "#25F4EE",    # ciano da marca
}
_PALETA_EXTRA = ["#4C78A8", "#72B7B2", "#54A24B", "#B279A2", "#F58518", "#9D755D"]


def _cores_para_categorias(categorias: list[str]) -> alt.Scale:
    """Cores fixas pros marketplaces conhecidos (senao Meli/Shopee/Amazon
    saiam todos em tons de azul do esquema padrao do Altair, dificeis de
    distinguir a olho) - categoria desconhecida cai numa paleta extra."""
    dominio, cores, i = [], [], 0
    for cat in categorias:
        dominio.append(cat)
        if cat in _CORES_MARKETPLACE:
            cores.append(_CORES_MARKETPLACE[cat])
        else:
            cores.append(_PALETA_EXTRA[i % len(_PALETA_EXTRA)])
            i += 1
    return alt.Scale(domain=dominio, range=cores)


def _grafico_pizza(df: pd.DataFrame, campo_categoria: str, campo_valor: str, titulo: str, cores: alt.Scale | None = None):
    """Grafico de pizza via Altair (ja vem junto com o Streamlit, nao
    precisa de dependencia nova) - st.bar_chart/st.altair_chart nativos do
    Streamlit nao tem tipo pizza."""
    cor_encoding = (
        alt.Color(f"{campo_categoria}:N", legend=alt.Legend(title=titulo), scale=cores)
        if cores is not None
        else alt.Color(f"{campo_categoria}:N", legend=alt.Legend(title=titulo))
    )
    base = alt.Chart(df).encode(
        theta=alt.Theta(f"{campo_valor}:Q", stack=True),
        color=cor_encoding,
        tooltip=[
            alt.Tooltip(f"{campo_categoria}:N", title=titulo),
            alt.Tooltip(f"{campo_valor}:Q", title="Valor", format=",.2f"),
        ],
    )
    return base.mark_arc(outerRadius=160).properties(height=420)


_GOLD = "#D9B559"
_GOLD_SOFT = "#8A7434"
_VERDE = "#3FBF6F"
_VERMELHO = "#E5484D"


def _injetar_tema_amo():
    """Skin preto/dourado do Grupo Amo por cima do tema 'dark' base do
    Streamlit (.streamlit/config.toml) - fontes (Baloo 2 pros títulos/
    números grandes, Manrope pro resto) + cartões de indicador (._kpi_card,
    verde quando o número é bom, vermelho piscando quando é ruim - mesmo
    padrão do alerta de "vendas abaixo do custo") + acento dourado nas
    abas. So cor/tipografia/polimento visual - layout e estrutura das
    telas continuam os mesmos."""
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@600;700;800&family=Manrope:wght@400;500;600;700&display=swap');
        html, body, [class*="css"] {{ font-family: "Manrope", ui-sans-serif, system-ui, sans-serif; }}
        h1, h2, h3 {{ font-family: "Baloo 2", "Manrope", sans-serif !important; }}

        [data-testid="stAppViewContainer"] {{
            background-image:
                radial-gradient(circle at 10% 0%, rgba(217,181,89,0.06), transparent 40%),
                radial-gradient(circle at 100% 100%, rgba(217,181,89,0.05), transparent 45%);
        }}

        .stTabs [data-baseweb="tab-list"] {{ gap: 4px; }}
        .stTabs [aria-selected="true"] {{ color: {_GOLD} !important; }}
        .stTabs [data-baseweb="tab-highlight"] {{ background-color: {_GOLD} !important; }}

        .kpi-card {{
            border-radius: 14px; padding: 14px 16px; margin-bottom: 8px;
            border: 1px solid rgba(217, 181, 89, 0.18); height: 100%;
            background-color: rgba(255, 255, 255, 0.02);
        }}
        .kpi-title {{ font-size: 0.80rem; font-weight: 600; opacity: 0.6; margin-bottom: 4px; }}
        .kpi-value {{ font-family: "Baloo 2", sans-serif; font-size: 1.7rem; font-weight: 700; line-height: 1.15; }}
        .kpi-sub {{ font-size: 0.78rem; margin-top: 4px; opacity: 0.9; }}
        .kpi-neutral .kpi-value {{ color: {_GOLD}; }}
        .kpi-green {{ background-color: rgba(63, 191, 111, 0.12); border-color: rgba(63, 191, 111, 0.35); }}
        .kpi-green .kpi-value, .kpi-green .kpi-sub {{ color: {_VERDE}; }}
        .kpi-red-blink {{
            background-color: rgba(229, 72, 77, 0.12); border-color: rgba(229, 72, 77, 0.4);
            animation: kpi_piscar 1.1s infinite;
        }}
        .kpi-red-blink .kpi-value, .kpi-red-blink .kpi-sub {{ color: {_VERMELHO}; }}
        @keyframes kpi_piscar {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.55; }} }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _kpi_card(titulo: str, valor: str, status: str = "neutral", sub: str | None = None):
    """status: 'green' (bom/positivo) | 'red' (ruim/negativo, pisca) | 'neutral' (informativo)."""
    classe = {"green": "kpi-green", "red": "kpi-red-blink", "neutral": "kpi-neutral"}[status]
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    st.markdown(
        f'<div class="kpi-card {classe}"><div class="kpi-title">{titulo}</div>'
        f'<div class="kpi-value">{valor}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )


# ----------------------------------------------------------------------
# Metas (faturamento/margem POR MES E POR CANAL - a mistura entre canais e
# uma decisao estrategica do time, ex.: "crescer Amazon, manter Shoppe 1",
# nao algo que da pra calcular olhando historico. A divisao por DIA dentro
# do mes/canal ja e automatica, via sazonalidade propria de cada canal.
# Hiper Meta e um segundo nivel, mais agressivo, que a planilha de
# referencia do time ja usa.)
# ----------------------------------------------------------------------

def carregar_metas() -> dict:
    """{mes (date, dia 1): {canal: {"meta_faturamento":..., "hiper_meta_faturamento":..., "meta_margem_pct":...}}}"""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_metas",
        params={"select": "mes,canal,meta_faturamento,hiper_meta_faturamento,meta_margem_pct"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=15,
    )
    resp.raise_for_status()
    metas: dict = {}
    for r in resp.json():
        mes = pd.to_datetime(r["mes"]).date()
        metas.setdefault(mes, {})[r["canal"]] = r
    return metas


def salvar_metas_canal(mes: date, registros: list[dict]) -> None:
    """`registros`: [{"canal":..., "meta_faturamento":..., "hiper_meta_faturamento":..., "meta_margem_pct":...}, ...]"""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    payload = [
        {
            "mes": mes.isoformat(),
            "canal": r["canal"],
            "meta_faturamento": r["meta_faturamento"],
            "hiper_meta_faturamento": r.get("hiper_meta_faturamento"),
            "meta_margem_pct": r["meta_margem_pct"],
            "atualizado_em": pd.Timestamp.utcnow().isoformat(),
        }
        for r in registros
    ]
    resp = requests.post(
        f"{url}/rest/v1/margin_monitor_metas?on_conflict=mes,canal",
        json=payload,
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
    faturamento de cada dia da semana nos ultimos `janela_dias`, normalizada
    pela media geral. Indice 1.0 = dia tipico; 1.3 = 30% acima da media.
    Mesmo metodo da planilha de metas de referencia do time ("Índices por
    dia da semana calculados sobre os dias com dado verificado")."""
    limite = pd.Timestamp(_hoje_brasilia() - timedelta(days=janela_dias))
    hist = pedidos_hist[pedidos_hist["data_pedido_dt"] >= limite]
    if hist.empty:
        return {i: 1.0 for i in range(7)}

    por_data = hist.groupby(hist["data_pedido_dt"].dt.date)["valor_venda_efetivo"].sum()
    por_data.index = pd.to_datetime(por_data.index)
    media_por_dia_semana = por_data.groupby(por_data.index.dayofweek).mean()
    media_geral = media_por_dia_semana.mean()
    if not media_geral:
        return {i: 1.0 for i in range(7)}

    indices = (media_por_dia_semana / media_geral).to_dict()
    return {i: indices.get(i, 1.0) for i in range(7)}


def calcular_indices_por_canal(pedidos_hist: pd.DataFrame, canais: list[str], janela_dias: int = 90) -> dict[str, dict[int, float]]:
    """Sazonalidade calculada SEPARADAMENTE por canal - cada marketplace tem
    seu proprio padrao de dia da semana (ex.: Shopee pode ter pico em dia de
    campanha diferente do Mercado Livre)."""
    return {
        canal: calcular_indices_dia_semana(pedidos_hist[pedidos_hist["canal"] == canal], janela_dias)
        for canal in canais
    }


def calcular_mix_canal(pedidos_hist: pd.DataFrame, canais: list[str], janela_dias: int = 90) -> dict[str, float]:
    """Participação (0 a 1) de cada canal no faturamento dos últimos
    `janela_dias` - usado só pra PRE-PREENCHER a meta por canal a partir de
    1 meta total (ponto de partida a partir do histórico real de vendas);
    o time ainda pode ajustar linha a linha depois pra refletir decisão
    estratégica (ex.: crescer um canal de propósito)."""
    limite = pd.Timestamp(_hoje_brasilia() - timedelta(days=janela_dias))
    hist = pedidos_hist[(pedidos_hist["data_pedido_dt"] >= limite) & (pedidos_hist["canal"].isin(canais))]
    if hist.empty:
        return {canal: 1 / len(canais) for canal in canais} if canais else {}
    por_canal = hist.groupby("canal")["valor_venda_efetivo"].sum()
    total = por_canal.sum()
    if not total:
        return {canal: 1 / len(canais) for canal in canais} if canais else {}
    return {canal: por_canal.get(canal, 0.0) / total for canal in canais}


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
    data_inicio: date, data_fim: date, metas_por_mes: dict, indices_por_canal: dict
) -> tuple[float, float, bool]:
    """Soma a meta de TODOS os canais, cada um repartido pelos dias do
    periodo pedido proporcional ao indice de sazonalidade proprio daquele
    canal. Retorna (meta_faturamento, meta_margem_r$, tem_mes_sem_meta)."""
    total_fat = 0.0
    total_margem = 0.0
    faltando = False
    for mes in _meses_no_intervalo(data_inicio, data_fim):
        metas_canais = metas_por_mes.get(mes)
        if not metas_canais:
            faltando = True
            continue
        dias_mes = _dias_do_mes(mes)
        for canal, meta_canal in metas_canais.items():
            indices_canal = indices_por_canal.get(canal) or {i: 1.0 for i in range(7)}
            soma_pesos_mes = sum(indices_canal[d.weekday()] for d in dias_mes)
            if not soma_pesos_mes:
                continue
            for d in dias_mes:
                if data_inicio <= d <= data_fim:
                    peso_dia = indices_canal[d.weekday()] / soma_pesos_mes
                    meta_dia_fat = (meta_canal.get("meta_faturamento") or 0.0) * peso_dia
                    total_fat += meta_dia_fat
                    total_margem += meta_dia_fat * (meta_canal.get("meta_margem_pct") or 0.0) / 100
    return round(total_fat, 2), round(total_margem, 2), faltando


def calcular_projecao_mes(
    pedidos_hist: pd.DataFrame,
    mes: date,
    metas_canais: dict | None,
    indices_por_canal: dict,
    campo: str = "valor_venda_efetivo",
    campo_meta: str = "meta_faturamento",
) -> tuple[float | None, float | None]:
    """Projeta o MES INTEIRO (nao so o periodo filtrado na sidebar) pra
    qualquer coluna numerica de `pedidos_hist` (faturamento via
    "valor_venda_efetivo", margem via "margem_contribuicao"), canal por
    canal: calcula quanto cada "unidade de peso de sazonalidade" daquele
    canal valeu em R$ nos dias ja passados do mes, e aplica esse mesmo valor
    aos dias que faltam - depois soma todos os canais. `campo_meta` escolhe
    a meta de comparacao: "meta_faturamento"/"hiper_meta_faturamento" (ja
    e um valor em R$) ou "meta_margem_valor" (derivado aqui como
    meta_faturamento * meta_margem_pct / 100, ja que o cadastro so guarda a
    margem em %, nao em R$). Retorna (projecao_r$, pct_da_meta) - qualquer
    um pode vir None se nao houver dado/meta suficiente."""
    hoje = _hoje_brasilia()
    dias_mes = _dias_do_mes(mes)
    dias_passados = [d for d in dias_mes if d <= hoje]
    dias_futuros = [d for d in dias_mes if d > hoje]
    if not dias_passados:
        return None, None

    pedidos_mes = pedidos_hist[
        (pedidos_hist["data_pedido_dt"].dt.date >= dias_mes[0])
        & (pedidos_hist["data_pedido_dt"].dt.date <= min(hoje, dias_mes[-1]))
    ]
    if pedidos_mes.empty:
        return None, None

    projecao_total = 0.0
    for canal, grupo in pedidos_mes.groupby("canal"):
        indices_canal = indices_por_canal.get(canal) or {i: 1.0 for i in range(7)}
        soma_pesos_passados = sum(indices_canal[d.weekday()] for d in dias_passados)
        soma_pesos_futuros = sum(indices_canal[d.weekday()] for d in dias_futuros)
        if not soma_pesos_passados:
            continue
        realizado_canal = grupo[campo].sum()
        valor_por_peso = realizado_canal / soma_pesos_passados
        projecao_total += realizado_canal + valor_por_peso * soma_pesos_futuros

    pct_meta = None
    if metas_canais:
        if campo_meta == "meta_margem_valor":
            meta_total = sum(
                (m.get("meta_faturamento") or 0.0) * (m.get("meta_margem_pct") or 0.0) / 100
                for m in metas_canais.values()
            )
        else:
            meta_total = sum(m.get(campo_meta) or 0.0 for m in metas_canais.values())
        if meta_total:
            pct_meta = round(projecao_total / meta_total * 100, 1)

    return round(projecao_total, 2), pct_meta


_DIAS_SEMANA_PT = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


def montar_relatorio_diario(
    pedidos_hist: pd.DataFrame, mes: date, metas_canais: dict, indices_por_canal: dict, canais_ordem: list[str]
) -> pd.DataFrame:
    """Reconstroi a planilha de metas diaria (1 linha por dia+canal, mais
    uma linha de total do dia) 100% a partir do Supabase - substitui o
    preenchimento manual de Realizado/Pedidos/Tkmedio/Gap/Acumulados."""
    dias_mes = _dias_do_mes(mes)
    hoje = _hoje_brasilia()
    pedidos_mes = pedidos_hist[
        (pedidos_hist["data_pedido_dt"].dt.date >= dias_mes[0]) & (pedidos_hist["data_pedido_dt"].dt.date <= dias_mes[-1])
    ]

    # Projecao do mes inteiro por canal (um valor so, repetido em todas as
    # linhas daquele canal - nao recalcula "como estava visto daquele dia",
    # so o que da pra saber HOJE).
    projecao_por_canal: dict[str, float] = {}
    for canal in canais_ordem:
        proj, _ = calcular_projecao_mes(pedidos_hist, mes, {canal: metas_canais[canal]} if canal in metas_canais else None, indices_por_canal)
        projecao_por_canal[canal] = proj

    linhas = []
    acumulado = {canal: {"meta": 0.0, "hiper": 0.0, "realizado": 0.0} for canal in canais_ordem}

    for i, dia in enumerate(dias_mes, start=1):
        pedidos_dia = pedidos_mes[pedidos_mes["data_pedido_dt"].dt.date == dia]
        tot = {"meta": 0.0, "hiper": 0.0, "realizado": 0.0, "pedidos": 0}

        for canal in canais_ordem:
            meta_canal = metas_canais.get(canal, {})
            indices_canal = indices_por_canal.get(canal) or {i: 1.0 for i in range(7)}
            soma_pesos_mes = sum(indices_canal[d.weekday()] for d in dias_mes) or 1.0
            peso_dia = indices_canal[dia.weekday()] / soma_pesos_mes

            meta_dia = (meta_canal.get("meta_faturamento") or 0.0) * peso_dia
            hiper_dia = (meta_canal.get("hiper_meta_faturamento") or 0.0) * peso_dia

            pedidos_canal_dia = pedidos_dia[pedidos_dia["canal"] == canal]
            realizado_dia = pedidos_canal_dia["valor_venda_efetivo"].sum()
            n_pedidos = len(pedidos_canal_dia)

            acumulado[canal]["meta"] += meta_dia
            acumulado[canal]["hiper"] += hiper_dia
            if dia <= hoje:
                acumulado[canal]["realizado"] += realizado_dia

            linhas.append(
                {
                    "dia": i,
                    "data": dia.strftime("%d/%m"),
                    "dia_semana": _DIAS_SEMANA_PT[dia.weekday()],
                    "canal": canal,
                    "meta": round(meta_dia, 2),
                    "hiper_meta": round(hiper_dia, 2),
                    "realizado": round(realizado_dia, 2) if dia <= hoje else None,
                    "gap": round(realizado_dia - meta_dia, 2) if dia <= hoje else None,
                    "pedidos": n_pedidos if dia <= hoje else None,
                    "tkmedio": round(realizado_dia / n_pedidos, 2) if (dia <= hoje and n_pedidos) else None,
                    "projeção": round(projecao_por_canal.get(canal), 2) if projecao_por_canal.get(canal) is not None else None,
                    "gap_acumulado": round(acumulado[canal]["realizado"] - acumulado[canal]["meta"], 2) if dia <= hoje else None,
                    "acumulado_meta": round(acumulado[canal]["meta"], 2),
                    "acumulado_hiper_meta": round(acumulado[canal]["hiper"], 2),
                    "acumulado_realizado": round(acumulado[canal]["realizado"], 2) if dia <= hoje else None,
                }
            )

            tot["meta"] += meta_dia
            tot["hiper"] += hiper_dia
            tot["realizado"] += realizado_dia
            tot["pedidos"] += n_pedidos

        acumulado_meta_total = sum(a["meta"] for a in acumulado.values())
        acumulado_hiper_total = sum(a["hiper"] for a in acumulado.values())
        acumulado_realizado_total = sum(a["realizado"] for a in acumulado.values())
        linhas.append(
            {
                "dia": i,
                "data": dia.strftime("%d/%m"),
                "dia_semana": _DIAS_SEMANA_PT[dia.weekday()],
                "canal": "TOTAL DO DIA",
                "meta": round(tot["meta"], 2),
                "hiper_meta": round(tot["hiper"], 2),
                "realizado": round(tot["realizado"], 2) if dia <= hoje else None,
                "gap": round(tot["realizado"] - tot["meta"], 2) if dia <= hoje else None,
                "pedidos": tot["pedidos"] if dia <= hoje else None,
                "tkmedio": round(tot["realizado"] / tot["pedidos"], 2) if (dia <= hoje and tot["pedidos"]) else None,
                "projeção": round(sum(v for v in projecao_por_canal.values() if v is not None), 2),
                "gap_acumulado": round(acumulado_realizado_total - acumulado_meta_total, 2) if dia <= hoje else None,
                "acumulado_meta": round(acumulado_meta_total, 2),
                "acumulado_hiper_meta": round(acumulado_hiper_total, 2),
                "acumulado_realizado": round(acumulado_realizado_total, 2) if dia <= hoje else None,
            }
        )

    return pd.DataFrame(linhas)


_injetar_tema_amo()
st.title("📊 Margem de contribuição — Grupo Amo")

_LIMIAR_ATRASO_MIN = 20  # ciclo roda a cada 15min "de calendario" - ver monitor.yml
_LIMIAR_CRITICO_MIN = 60


def obter_checkpoints() -> pd.DataFrame:
    """Le margin_monitor_checkpoint (1 linha por conta Tiny com a hora da
    ultima sincronizacao bem-sucedida do monitor) - so pra mostrar "ultima
    atualizacao" no topo do dashboard, nao entra em nenhum calculo de
    margem/faturamento. Tabela tem RLS travada por padrao (so service role)
    - precisa da policy de SELECT pra anon em supabase_schema_checkpoint_leitura.sql."""
    url = _config("SUPABASE_URL").rstrip("/")
    key = _config("SUPABASE_ANON_KEY")
    resp = requests.get(
        f"{url}/rest/v1/margin_monitor_checkpoint",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"select": "*"},
        timeout=15,
    )
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


col_status, col_botao = st.columns([5, 1])
with col_status:
    try:
        checkpoints = obter_checkpoints()
    except Exception:
        checkpoints = None
    if checkpoints is None:
        st.caption("⚪ Não foi possível checar a última sincronização.")
    elif checkpoints.empty:
        st.caption("⚪ Sem dado de sincronização ainda - a policy de leitura foi cadastrada? Ver supabase_schema_checkpoint_leitura.sql.")
    else:
        checkpoints["quando"] = pd.to_datetime(checkpoints["ultima_busca_iso"], utc=True)
        mais_antigo = checkpoints["quando"].min()
        minutos_atras = (pd.Timestamp.now(tz="UTC") - mais_antigo).total_seconds() / 60
        if minutos_atras < _LIMIAR_ATRASO_MIN:
            cor, rotulo = "🟢", "em dia"
        elif minutos_atras < _LIMIAR_CRITICO_MIN:
            cor, rotulo = "🟡", "atrasando"
        else:
            cor, rotulo = "🔴", "atrasado"
        tempo_fmt = f"há {int(minutos_atras)} min" if minutos_atras < 60 else f"há {minutos_atras / 60:.1f}h"
        with st.expander(f"{cor} Última sincronização: {tempo_fmt} ({rotulo}) - detalhar por conta"):
            for _, linha in checkpoints.sort_values("quando").iterrows():
                hora_brt = (linha["quando"] - pd.Timedelta(hours=3)).strftime("%d/%m %H:%M")
                st.caption(f"{linha['conta_tiny']}: {hora_brt} (horário de Brasília)")
with col_botao:
    if st.button("🔄 Atualizar", help="Limpa o cache do dashboard e relê o Supabase agora (o cache normal dura 2min)."):
        st.cache_data.clear()
        st.rerun()

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

# valor_venda (bruto) e a base certa pra comparar com "Realizado" de
# relatorios externos - confirmado comparando pedido a pedido contra a
# planilha de metas do time (2026-09-19): receita liquida so bate pros
# canais que nao sao Mercado Livre. Pedidos antigos sem valor_venda gravado
# caem pra receita (aproximacao - ver dashboard, aba Vendas).
if "valor_venda" not in pedidos.columns:
    pedidos["valor_venda"] = None
pedidos["valor_venda_efetivo"] = pedidos["valor_venda"].fillna(pedidos["receita"])

# ----------------------------------------------------------------------
# Filtros (sidebar)
# ----------------------------------------------------------------------

st.sidebar.header("Filtros")

datas_validas = pedidos["data_pedido_dt"].dropna()
data_min_dados = datas_validas.min().date() if not datas_validas.empty else _hoje_brasilia()
data_max_dados = datas_validas.max().date() if not datas_validas.empty else _hoje_brasilia()

hoje = _hoje_brasilia()
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

aba_visao, aba_metas, aba_produtos, aba_custos, aba_vendas = st.tabs(
    ["📈 Visão geral", "📅 Metas diárias", "📦 Produtos", "⚠️ Custos pendentes", "🧾 Vendas"]
)

canais_todos = sorted(pedidos["canal"].dropna().unique())

# ---- Visão geral -------------------------------------------------------
with aba_visao:
    # -- Meta do mes: cadastro (por canal) + comparativo com o realizado --
    try:
        metas_por_mes = carregar_metas()
    except Exception:
        metas_por_mes = {}

    with st.expander("🎯 Meta do mês (por canal, faturamento + margem)"):
        mes_config = st.date_input(
            "Configurar meta de qual mês?", value=_hoje_brasilia().replace(day=1), format="DD/MM/YYYY", key="mes_config_visao"
        ).replace(day=1)
        metas_existentes = metas_por_mes.get(mes_config, {})
        chave_autofill = f"metas_autofill_{mes_config.isoformat()}"

        st.markdown("**Preencher a partir de 1 meta total** (opcional - divide pelo mix histórico de canal dos últimos 90 dias)")
        ct1, ct2, ct3, ct4 = st.columns([2, 2, 1.5, 1.5])
        meta_total_input = ct1.number_input("Meta TOTAL do mês (R$)", min_value=0.0, step=1000.0, key=f"meta_total_{mes_config}")
        hiper_total_input = ct2.number_input("Hiper Meta TOTAL (R$)", min_value=0.0, step=1000.0, key=f"hiper_total_{mes_config}")
        margem_geral_input = ct3.number_input(
            "Margem geral (%)", min_value=0.0, max_value=100.0, step=0.5, value=15.0, key=f"margem_geral_{mes_config}"
        )
        ct4.write("")
        ct4.write("")
        if ct4.button("🔄 Dividir por canal"):
            mix = calcular_mix_canal(pedidos, canais_todos)
            st.session_state[chave_autofill] = {
                canal: {
                    "meta_faturamento": round(meta_total_input * mix.get(canal, 0.0), 2),
                    "hiper_meta_faturamento": round(hiper_total_input * mix.get(canal, 0.0), 2),
                    "meta_margem_pct": margem_geral_input,
                }
                for canal in canais_todos
            }
            st.rerun()

        fonte_metas = st.session_state.get(chave_autofill) or metas_existentes
        tabela_metas = pd.DataFrame(
            [
                {
                    "canal": canal,
                    "meta_faturamento": float(fonte_metas.get(canal, {}).get("meta_faturamento") or 0.0),
                    "hiper_meta_faturamento": float(fonte_metas.get(canal, {}).get("hiper_meta_faturamento") or 0.0),
                    "meta_margem_pct": float(fonte_metas.get(canal, {}).get("meta_margem_pct") or 15.0),
                }
                for canal in canais_todos
            ]
        )
        st.caption(
            "'Dividir por canal' usa o histórico só como PONTO DE PARTIDA - ajuste linha a linha se quiser "
            "crescer/reduzir algum canal de propósito antes de salvar. A divisão por DIA dentro do mês "
            "(dentro de cada canal) é sempre automática, proporcional a como aquele canal costuma vender "
            "por dia da semana."
        )
        metas_editadas = st.data_editor(
            tabela_metas,
            hide_index=True,
            use_container_width=True,
            column_config={
                "canal": st.column_config.TextColumn("Canal", disabled=True),
                "meta_faturamento": st.column_config.NumberColumn("Meta (R$)", min_value=0.0, step=100.0),
                "hiper_meta_faturamento": st.column_config.NumberColumn("Hiper Meta (R$)", min_value=0.0, step=100.0),
                "meta_margem_pct": st.column_config.NumberColumn("Meta de margem (%)", min_value=0.0, max_value=100.0, step=0.5),
            },
            key="editor_metas_visao",
        )
        if st.button("💾 Salvar metas do mês"):
            try:
                registros = metas_editadas.to_dict("records")
                salvar_metas_canal(mes_config, registros)
                st.session_state.pop(chave_autofill, None)
                st.success(f"Metas de {mes_config.strftime('%m/%Y')} salvas ({len(registros)} canal(is)).")
                st.rerun()
            except Exception as e:
                st.error(f"Falha ao salvar: {e}")

    indices_por_canal = calcular_indices_por_canal(pedidos, canais_todos)
    meta_fat_periodo, meta_margem_periodo, meta_incompleta = calcular_meta_periodo(
        data_inicio, data_fim, metas_por_mes, indices_por_canal
    )
    # Faturamento/margem real do periodo SEMPRE de todas as contas/canais
    # (a meta e um numero unico da empresa toda - nao faz sentido comparar
    # com um subconjunto filtrado na sidebar).
    pedidos_periodo_empresa = pedidos[
        (pedidos["data_pedido_dt"].dt.date >= data_inicio) & (pedidos["data_pedido_dt"].dt.date <= data_fim)
    ]
    faturamento_real = pedidos_periodo_empresa["valor_venda_efetivo"].sum()
    margem_real = pedidos_periodo_empresa["margem_contribuicao"].sum()

    itens_com_custo = itens_f[itens_f["custo_ausente"] == False]  # noqa: E712
    receita_com_custo = itens_com_custo["receita"].sum()
    margem_com_custo = itens_com_custo["margem_contribuicao"].sum()
    margem_pct_media = (margem_com_custo / receita_com_custo * 100) if receita_com_custo else 0

    mes_corrente = _hoje_brasilia().replace(day=1)
    projecao_fat_mes, pct_projecao_fat_meta = calcular_projecao_mes(
        pedidos, mes_corrente, metas_por_mes.get(mes_corrente), indices_por_canal,
        campo="valor_venda_efetivo", campo_meta="meta_faturamento",
    )
    projecao_margem_mes, pct_projecao_margem_meta = calcular_projecao_mes(
        pedidos, mes_corrente, metas_por_mes.get(mes_corrente), indices_por_canal,
        campo="margem_contribuicao", campo_meta="meta_margem_valor",
    )

    with st.container(border=True):
        st.caption("Faturamento e margem (com meta e projeção) SEMPRE somam todas as contas/canais, independente do filtro ao lado.")

        cm1, cm2, cm3, cm4 = st.columns(4)
        with cm1:
            _kpi_card("💰 Faturamento", _fmt_moeda(faturamento_real), "neutral")
        with cm2:
            pct_fat = (faturamento_real / meta_fat_periodo * 100) if meta_fat_periodo else None
            _kpi_card(
                "🎯 Meta de Faturamento",
                _fmt_moeda(meta_fat_periodo) if not meta_incompleta else "sem meta",
                "neutral",
                sub=(f"{pct_fat - 100:+.1f}% da meta" if pct_fat is not None else None),
            )
        with cm3:
            _kpi_card("📊 Margem", _fmt_moeda(margem_real), "green" if margem_real >= 0 else "red")
        with cm4:
            pct_margem = (margem_real / meta_margem_periodo * 100) if meta_margem_periodo else None
            _kpi_card(
                "🎯 Meta de Margem",
                _fmt_moeda(meta_margem_periodo) if not meta_incompleta else "sem meta",
                "neutral",
                sub=(f"{pct_margem - 100:+.1f}% da meta" if pct_margem is not None else None),
            )
        if meta_incompleta:
            st.caption("⚠️ Algum mês do período selecionado ainda não tem meta cadastrada acima.")

        cp1, cp2, cp3, cp4 = st.columns(4)
        status_fat_proj = "green" if (pct_projecao_fat_meta is None and (projecao_fat_mes or 0) >= 0) or (pct_projecao_fat_meta is not None and pct_projecao_fat_meta >= 100) else "red"
        with cp1:
            _kpi_card(
                f"📈 Projeção Faturamento ({mes_corrente.strftime('%m/%Y')})",
                _fmt_moeda(projecao_fat_mes) if projecao_fat_mes is not None else "sem dado suficiente",
                status_fat_proj if projecao_fat_mes is not None else "neutral",
            )
        with cp2:
            _kpi_card(
                "% da Meta (projeção fat.)",
                f"{pct_projecao_fat_meta:.1f}%" if pct_projecao_fat_meta is not None else "sem meta",
                status_fat_proj if pct_projecao_fat_meta is not None else "neutral",
                sub=(f"{pct_projecao_fat_meta - 100:+.1f}pp" if pct_projecao_fat_meta is not None else None),
            )
        status_margem_proj = "green" if (pct_projecao_margem_meta is None and (projecao_margem_mes or 0) >= 0) or (pct_projecao_margem_meta is not None and pct_projecao_margem_meta >= 100) else "red"
        with cp3:
            _kpi_card(
                f"📈 Projeção Margem ({mes_corrente.strftime('%m/%Y')})",
                _fmt_moeda(projecao_margem_mes) if projecao_margem_mes is not None else "sem dado suficiente",
                status_margem_proj if projecao_margem_mes is not None else "neutral",
            )
        with cp4:
            _kpi_card(
                "% da Meta (projeção margem)",
                f"{pct_projecao_margem_meta:.1f}%" if pct_projecao_margem_meta is not None else "sem meta",
                status_margem_proj if pct_projecao_margem_meta is not None else "neutral",
                sub=(f"{pct_projecao_margem_meta - 100:+.1f}pp" if pct_projecao_margem_meta is not None else None),
            )
        st.caption(
            "Projeção sempre do MÊS CORRENTE inteiro (independente do período filtrado ao lado): pega o ritmo real "
            "de venda/margem por dia da semana já observado este mês e extrapola pros dias que faltam. "
            "🟢 verde = na meta (ou positivo, se ainda sem meta cadastrada) · 🔴 vermelho piscando = abaixo da meta (ou negativo)."
        )

        st.divider()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Pedidos", f"{len(pedidos_f):,}".replace(",", "."))
        c2.metric("Receita total", _fmt_moeda(pedidos_f["receita"].sum()))
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
            if "numero_ecommerce" not in detalhe.columns:
                detalhe["numero_ecommerce"] = None

            def _sugestao(row) -> str:
                if pd.notna(row["custo_referencia"]) and abs(row["custo_unitario"] - row["custo_referencia"]) > 0.01:
                    return (
                        f"⚠️ Revisar CUSTO cadastrado - diverge dos irmãos "
                        f"(R$ {row['custo_unitario']:.2f} vs R$ {row['custo_referencia']:.2f} do grupo)"
                    )
                return "💲 Revisar PREÇO de venda ou pausar anúncio - custo cadastrado bate com os irmãos"

            detalhe["sugestão"] = detalhe.apply(_sugestao, axis=1)

            # data_editor (mesmo com tudo desabilitado) deixa selecionar e
            # copiar o texto de UMA celula so - st.dataframe normal so deixa
            # selecionar a linha inteira, dificultava copiar so o codigo do
            # anuncio ou o numero da venda no marketplace.
            st.data_editor(
                detalhe[
                    [
                        "numero_pedido", "numero_ecommerce", "canal", "codigo_pai", "sku", "anuncio_id",
                        "quantidade", "receita", "cmv", "perda", "sugestão",
                    ]
                ].sort_values("perda", ascending=False),
                hide_index=True,
                use_container_width=True,
                disabled=True,
                key="editor_abaixo_custo",
                column_config={
                    "numero_pedido": "Pedido (Tiny)",
                    "numero_ecommerce": "Nº da venda (marketplace)",
                    "canal": "Marketplace/conta",
                    "codigo_pai": "Código pai",
                    "anuncio_id": "Anúncio (ML)",
                    "receita": st.column_config.NumberColumn("Receita", format="R$ %.2f"),
                    "cmv": st.column_config.NumberColumn("CMV", format="R$ %.2f"),
                    "perda": st.column_config.NumberColumn("Perda (CMV − receita)", format="R$ %.2f"),
                },
            )
            st.caption(
                "Clique numa célula (ex.: Anúncio ou Nº da venda) e Ctrl+C pra copiar só aquele valor. "
                "'Nº da venda (marketplace)' e 'Anúncio (ML)' só aparecem pra vendas processadas depois "
                "desses recursos existirem - em branco = ainda não capturado (não tem como o dashboard "
                "buscar isso ao vivo)."
            )
    else:
        st.success("✅ Nenhuma venda abaixo do custo da mercadoria no período.")

    st.subheader("Faturamento e margem por marketplace")
    pedidos_f_mkt = pedidos_f.copy()
    pedidos_f_mkt["marketplace"] = pedidos_f_mkt["canal"].map(_marketplace_generico)
    por_mkt = (
        pedidos_f_mkt.groupby("marketplace")
        .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
        .reset_index()
    )
    por_mkt["margem_pct"] = (por_mkt["margem"] / por_mkt["receita"] * 100).round(1)
    por_mkt = por_mkt.sort_values("margem", ascending=False)
    por_mkt["margem_abs"] = por_mkt["margem"].abs()
    escala_mkt = _cores_para_categorias(por_mkt["marketplace"].tolist())

    pc1, pc2 = st.columns(2)
    with pc1:
        st.markdown("##### 💰 Faturamento por Marketplace")
        st.altair_chart(
            _grafico_pizza(por_mkt, "marketplace", "receita", "Marketplace", cores=escala_mkt),
            use_container_width=True,
        )
    with pc2:
        st.markdown("##### 📊 Margem por Marketplace")
        st.altair_chart(
            _grafico_pizza(por_mkt, "marketplace", "margem_abs", "Marketplace", cores=escala_mkt),
            use_container_width=True,
        )
        st.caption(
            "Tamanho da fatia = valor absoluto (margem negativa também vira fatia) - passe o mouse ou "
            "veja a tabela abaixo pro sinal real."
        )

    st.markdown("###### Detalhado por conta")
    pedidos_f_mkt["conta"] = pedidos_f_mkt["canal"].map(_nome_conta)
    por_conta = (
        pedidos_f_mkt.groupby(["marketplace", "conta"])
        .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "count"))
        .reset_index()
    )
    por_conta["margem_pct"] = (por_conta["margem"] / por_conta["receita"] * 100).round(1)
    por_conta = por_conta.sort_values(["marketplace", "margem"], ascending=[True, False])
    st.dataframe(
        por_conta[["marketplace", "conta", "receita", "margem", "margem_pct", "pedidos"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "marketplace": "Marketplace",
            "conta": "Conta",
            "receita": st.column_config.NumberColumn("Receita", format="R$ %.2f"),
            "margem": st.column_config.NumberColumn("Margem", format="R$ %.2f"),
            "margem_pct": st.column_config.NumberColumn("Margem %", format="%.1f%%"),
            "pedidos": "Pedidos",
        },
    )

    st.subheader("Faturamento e margem por categoria de produto")
    try:
        categorias_produto = carregar_categorias_produto()
    except Exception:
        categorias_produto = {}
        st.caption(
            "⚪ Categoria ainda não disponível - falta rodar supabase_schema_categorias.sql no Supabase."
        )

    itens_f_cat = itens_f.copy()
    itens_f_cat["codigo_pai"] = itens_f_cat["sku"].map(_codigo_pai)
    itens_f_cat["categoria"] = itens_f_cat["codigo_pai"].map(categorias_produto).fillna(SEM_CATEGORIA)
    por_categoria = (
        itens_f_cat.groupby("categoria")
        .agg(receita=("receita", "sum"), margem=("margem_contribuicao", "sum"), pedidos=("numero_pedido", "nunique"))
        .reset_index()
    )
    por_categoria["margem_pct"] = (por_categoria["margem"] / por_categoria["receita"] * 100).round(1)
    por_categoria = por_categoria.sort_values("margem", ascending=False)
    por_categoria["margem_abs"] = por_categoria["margem"].abs()
    escala_cat = _cores_para_categorias(por_categoria["categoria"].tolist())

    pc3, pc4 = st.columns(2)
    with pc3:
        st.markdown("##### 💰 Faturamento por Categoria")
        st.altair_chart(
            _grafico_pizza(por_categoria, "categoria", "receita", "Categoria", cores=escala_cat),
            use_container_width=True,
        )
    with pc4:
        st.markdown("##### 📊 Margem por Categoria")
        st.altair_chart(
            _grafico_pizza(por_categoria, "categoria", "margem_abs", "Categoria", cores=escala_cat),
            use_container_width=True,
        )
        st.caption(
            "Tamanho da fatia = valor absoluto (margem negativa também vira fatia) - passe o mouse ou "
            "veja a tabela abaixo pro sinal real."
        )
    st.caption(
        f'"{SEM_CATEGORIA}" = produto que ainda não vendeu pelo Mercado Livre (única fonte da categoria, '
        "via API) ou que ainda não foi classificado nesse ciclo."
    )
    st.dataframe(
        por_categoria[["categoria", "receita", "margem", "margem_pct", "pedidos"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "categoria": "Categoria",
            "receita": st.column_config.NumberColumn("Receita", format="R$ %.2f"),
            "margem": st.column_config.NumberColumn("Margem", format="R$ %.2f"),
            "margem_pct": st.column_config.NumberColumn("Margem %", format="%.1f%%"),
            "pedidos": "Pedidos",
        },
    )

# ---- Metas diárias --------------------------------------------------------
with aba_metas:
    st.caption(
        "Recria a planilha de metas diária automaticamente a partir do Supabase - Realizado, Pedidos, "
        "Tkmedio, Gap, Projeção e Acumulados não precisam mais ser preenchidos à mão todo dia. "
        "Só a Meta e a Hiper Meta de cada canal são cadastradas (aba Visão geral), 1 vez por mês."
    )
    mes_relatorio = st.date_input(
        "Mês do relatório", value=_hoje_brasilia().replace(day=1), format="DD/MM/YYYY", key="mes_relatorio_diario"
    ).replace(day=1)

    metas_mes_relatorio = metas_por_mes.get(mes_relatorio, {})
    if not metas_mes_relatorio:
        st.warning(
            f"Nenhuma meta cadastrada pra {mes_relatorio.strftime('%m/%Y')} ainda - cadastre na aba "
            "'Visão geral' (expander 'Meta do mês'). O relatório abaixo funciona mesmo sem meta "
            "(fica só com Realizado/Pedidos/Tkmedio), mas Meta/Gap/Projeção ficam zerados."
        )

    relatorio = montar_relatorio_diario(pedidos, mes_relatorio, metas_mes_relatorio, indices_por_canal, canais_todos)

    st.dataframe(
        relatorio,
        hide_index=True,
        use_container_width=True,
        height=600,
        column_config={
            "dia": st.column_config.NumberColumn("//"),
            "data": "Data",
            "dia_semana": "Dia/Semana",
            "canal": "Marketplace",
            "meta": st.column_config.NumberColumn("Meta", format="R$ %.2f"),
            "hiper_meta": st.column_config.NumberColumn("Hiper Meta", format="R$ %.2f"),
            "realizado": st.column_config.NumberColumn("Realizado", format="R$ %.2f"),
            "gap": st.column_config.NumberColumn("Gap", format="R$ %.2f"),
            "pedidos": st.column_config.NumberColumn("Pedidos"),
            "tkmedio": st.column_config.NumberColumn("Tkmédio", format="R$ %.2f"),
            "projeção": st.column_config.NumberColumn("Projeção", format="R$ %.2f"),
            "gap_acumulado": st.column_config.NumberColumn("Gap Acumulado", format="R$ %.2f"),
            "acumulado_meta": st.column_config.NumberColumn("Acumulado Meta", format="R$ %.2f"),
            "acumulado_hiper_meta": st.column_config.NumberColumn("Acumulado Hiper Meta", format="R$ %.2f"),
            "acumulado_realizado": st.column_config.NumberColumn("Acumulado Realizado", format="R$ %.2f"),
        },
    )

    buffer_relatorio = io.BytesIO()
    relatorio.to_excel(buffer_relatorio, index=False, sheet_name=mes_relatorio.strftime("%B %Y"))
    st.download_button(
        "⬇️ Baixar relatório (Excel)",
        buffer_relatorio.getvalue(),
        f"metas_{mes_relatorio.strftime('%Y_%m')}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

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

    # numero_ecommerce (nº da venda no marketplace) so existe em
    # margin_monitor_itens, nao em margin_monitor_pedidos - traz pra ca via
    # merge (1 pedido pode ter varios itens, mas todos com o mesmo
    # numero_ecommerce, por isso o drop_duplicates).
    if "numero_ecommerce" in itens.columns:
        mapa_ecommerce = itens[["conta_tiny", "numero_pedido", "numero_ecommerce"]].drop_duplicates(
            subset=["conta_tiny", "numero_pedido"]
        )
        tabela_vendas = tabela_vendas.merge(mapa_ecommerce, on=["conta_tiny", "numero_pedido"], how="left")
    else:
        tabela_vendas["numero_ecommerce"] = None
    tabela_vendas["numero_ecommerce"] = tabela_vendas["numero_ecommerce"].fillna("")

    # processado_em = hora em que ESTE pedido foi gravado pela 1a vez no
    # Supabase (default now() na coluna, nao muda em upsert - ver
    # supabase_schema.sql) - e o jeito de validar "isso realmente acabou de
    # sincronizar" direto na tabela, ja que data_pedido do Tiny so tem DIA,
    # sem horario.
    if "processado_em" in tabela_vendas.columns:
        tabela_vendas["sincronizado_em"] = (
            pd.to_datetime(tabela_vendas["processado_em"], utc=True, errors="coerce") - pd.Timedelta(hours=3)
        ).dt.strftime("%d/%m %H:%M")
    else:
        tabela_vendas["sincronizado_em"] = None

    st.caption(
        "Valor Venda → (-) CMV → (-) Imposto → (-) Comissão → (-) Frete → (-) Ads → (=) Margem. "
        "Pro Mercado Livre, Comissão/Frete são o valor REAL cobrado (via API do ML), não estimativa - "
        "os outros canais usam % estimado (Tiny não expõe o valor exato cobrado pelo marketplace). "
        "Vazio = pedido lançado antes desse detalhamento existir. \"Sincronizado em\" é quando ESTE "
        "sistema gravou o pedido (horário de Brasília) - o Tiny não expõe a hora da venda, só a data."
    )

    colunas_exibir = [
        "numero_pedido", "numero_ecommerce", "conta_tiny", "canal", "data_pedido", "sincronizado_em",
        "valor_venda", "cmv", "imposto", "comissao", "frete", "ads", "margem_contribuicao", "margem_pct",
        "custo_ausente", "alertado",
    ]
    st.dataframe(
        tabela_vendas[colunas_exibir],
        hide_index=True,
        use_container_width=True,
        column_config={
            "numero_pedido": "Pedido (Tiny)",
            "numero_ecommerce": "Nº da venda (marketplace)",
            "sincronizado_em": "Sincronizado em",
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
