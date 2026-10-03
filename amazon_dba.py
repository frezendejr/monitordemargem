"""Tarifa logistica do Delivery by Amazon (DBA) - tabela oficial copiada de
https://venda.amazon.com.br/cresca/dba em 03/10/2026 (a pagina nao informa
vigencia - rever se a Amazon reajustar).

Regras (pagina + guia do vendedor):
- Tarifa POR UNIDADE; depende do preco do produto, do peso e do ESTADO/REGIAO
  DE ORIGEM do envio (nao do destino do cliente).
- Ate R$ 78,99: valor fixo por faixa de preco (sem peso/regiao).
- R$ 79 a R$ 199,99 e a partir de R$ 200: tabelas por faixa de peso x origem.
- Peso considerado = maior entre peso real e peso cubico (C x L x A em cm /
  6000), mais 20 g de embalagem; limite 30 kg.
"""

from __future__ import annotations

ORIGENS = ("sp_capital", "demais_capitais_sul_sudeste", "interior_sul_sudeste", "centro_oeste_norte_nordeste")

# faixa de peso -> tarifa em R$ por origem (ordem de ORIGENS)
_TABELA_79_199 = {
    "0-250g": (11.95, 13.95, 15.95, 17.95),
    "250-500g": (12.85, 15.00, 17.15, 19.30),
    "500g-1kg": (13.45, 15.70, 17.95, 20.20),
    "1-2kg": (14.00, 16.35, 18.75, 21.10),
    "2-3kg": (14.95, 17.45, 19.95, 22.40),
    "3-4kg": (16.15, 18.85, 21.55, 24.20),
    "4-5kg": (17.00, 19.90, 22.75, 25.60),
    "5-6kg": (25.00, 30.00, 34.00, 38.00),
    "6-7kg": (26.00, 31.00, 35.00, 39.00),
    "7-8kg": (27.00, 32.00, 36.00, 40.00),
    "8-9kg": (28.00, 33.00, 37.00, 41.00),
    "9-10kg": (39.50, 46.00, 52.75, 59.00),
}
_TABELA_200_MAIS = {
    "0-250g": (19.95, 19.95, 20.45, 20.45),
    "250-500g": (20.45, 20.45, 20.95, 20.95),
    "500g-1kg": (21.45, 21.45, 21.95, 21.95),
    "1-2kg": (22.95, 22.95, 23.45, 23.45),
    "2-3kg": (23.95, 23.95, 24.45, 24.45),
    "3-4kg": (25.95, 25.95, 25.95, 25.95),
    "4-5kg": (27.95, 27.95, 27.95, 27.95),
    "5-6kg": (36.95, 34.45, 36.95, 36.95),
    "6-7kg": (39.45, 36.95, 39.45, 39.45),
    "7-8kg": (40.45, 40.45, 40.45, 40.45),
    "8-9kg": (45.45, 46.95, 46.95, 46.95),
    "9-10kg": (59.95, 61.45, 65.95, 65.95),
}


def tarifa_dba(preco_unitario: float, faixa_peso: str, origem: str) -> float:
    """Tarifa DBA em R$ por unidade vendida a `preco_unitario` (preco do produto)."""
    if preco_unitario < 30:
        return 4.50
    if preco_unitario < 50:
        return 6.50
    if preco_unitario < 79:
        return 6.75
    tabela = _TABELA_79_199 if preco_unitario < 200 else _TABELA_200_MAIS
    return tabela[faixa_peso][ORIGENS.index(origem)]
