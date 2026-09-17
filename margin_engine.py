"""Calculo da margem de contribuicao por pedido.

Formula:
    MC = Receita - CMV - Imposto - Comissao/tarifa do canal - Frete - Ads

Imposto, Comissao, Frete e Ads sao SEMPRE percentuais estimados de
canal_config sobre a receita - nunca os valores exatos cobrados no pedido.
Isso e proposital: o Tiny nao expoe o valor exato de tarifa/frete cobrado
pelo marketplace em cada pedido, e alguns canais (confirmado no Mercado
Livre) reembolsam parte de cupons/bonus ao vendedor por fora do pedido (um
"estorno" que so aparece no extrato do proprio marketplace, nunca no JSON do
Tiny) - tentar reconciliar isso pedido a pedido exigiria integrar com a API
de cada marketplace, fora do escopo deste monitor. Por isso a margem aqui e
sempre uma ESTIMATIVA rapida para alertar prejuizo, nao uma reconciliacao
contabil exata.

Receita usa `total_pedido` do proprio Tiny (ja liquido de qualquer desconto
que o Tiny enxergue, como no TikTok Shop) em vez de recalcular a partir dos
itens - ver ResultadoMargem/calcular_margem.
"""

from dataclasses import dataclass, field


@dataclass
class ItemPedido:
    sku: str
    quantidade: float
    valor_unitario: float
    custo_unitario: float | None  # None = custo nao encontrado no cadastro


@dataclass
class ResultadoMargem:
    numero_pedido: str
    canal: str
    receita: float
    cmv: float
    imposto: float
    comissao: float
    frete: float
    ads: float
    margem_contribuicao: float
    margem_pct: float
    custo_ausente: bool
    skus_sem_custo: list[str] = field(default_factory=list)


def _percentual_frete(receita: float, canal_config: dict) -> float:
    """Resolve o % de frete aplicavel, usando faixas_frete quando existir."""
    faixas = canal_config.get("faixas_frete")
    if not faixas:
        return canal_config.get("frete_pct", 0.0)

    for faixa in faixas:
        teto = faixa.get("ate_valor")
        if teto is None or receita <= teto:
            return faixa["frete_pct"]

    # Nao deveria chegar aqui se a ultima faixa tiver ate_valor: null,
    # mas por seguranca usa a ultima faixa cadastrada.
    return faixas[-1]["frete_pct"]


def calcular_margem(
    numero_pedido: str,
    canal: str,
    itens: list[ItemPedido],
    receita: float,
    canal_config: dict,
) -> ResultadoMargem:
    """Calcula a margem de contribuicao de um pedido.

    `receita` deve vir de `pedido_completo["total_pedido"]` (Tiny) - ja e o
    valor final do pedido, liquido de qualquer desconto que o Tiny capture.
    O frete que o CANAL cobra do vendedor vem de canal_config (frete_pct
    fixo ou faixas_frete por valor do pedido), nao do valor_frete do pedido.
    """
    skus_sem_custo = [item.sku for item in itens if not item.custo_unitario]
    custo_ausente = len(skus_sem_custo) > 0

    cmv = sum(
        item.quantidade * (item.custo_unitario or 0.0)
        for item in itens
    )

    imposto_pct = canal_config.get("imposto_pct", 0.0)
    ads_pct = canal_config.get("ads_pct", 0.0)
    comissao_pct = canal_config.get("comissao_pct", 0.0)
    frete_pct = _percentual_frete(receita, canal_config)

    imposto = receita * imposto_pct / 100
    comissao = receita * comissao_pct / 100
    frete = receita * frete_pct / 100
    ads = receita * ads_pct / 100

    margem_contribuicao = receita - cmv - imposto - comissao - frete - ads
    margem_pct = (margem_contribuicao / receita * 100) if receita else 0.0

    return ResultadoMargem(
        numero_pedido=numero_pedido,
        canal=canal,
        receita=round(receita, 2),
        cmv=round(cmv, 2),
        imposto=round(imposto, 2),
        comissao=round(comissao, 2),
        frete=round(frete, 2),
        ads=round(ads, 2),
        margem_contribuicao=round(margem_contribuicao, 2),
        margem_pct=round(margem_pct, 2),
        custo_ausente=custo_ausente,
        skus_sem_custo=skus_sem_custo,
    )
