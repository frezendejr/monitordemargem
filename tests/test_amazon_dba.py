import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from amazon_dba import ORIGENS, tarifa_dba


def test_faixas_de_preco_fixas_abaixo_de_79():
    assert tarifa_dba(29.99, "9-10kg", "interior_sul_sudeste") == 4.50  # peso/origem nao importam
    assert tarifa_dba(30.00, "0-250g", "sp_capital") == 6.50
    assert tarifa_dba(78.99, "2-3kg", "centro_oeste_norte_nordeste") == 6.75


def test_valores_conferidos_com_a_simulacao_e_a_tabela():
    assert tarifa_dba(139.99, "2-3kg", "interior_sul_sudeste") == 19.95  # simulacao do minibike
    assert tarifa_dba(79.00, "0-250g", "sp_capital") == 11.95
    assert tarifa_dba(199.99, "1-2kg", "interior_sul_sudeste") == 18.75
    assert tarifa_dba(200.00, "1-2kg", "interior_sul_sudeste") == 23.45  # salto na virada de R$ 200


def test_tabelas_cobrem_todas_as_origens_e_faixas():
    for faixa in ("0-250g", "500g-1kg", "5-6kg", "9-10kg"):
        for origem in ORIGENS:
            assert tarifa_dba(150, faixa, origem) > 0 and tarifa_dba(250, faixa, origem) > 0


def test_origem_invalida_levanta_erro():
    with pytest.raises(ValueError):
        tarifa_dba(150, "1-2kg", "marte")
