import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook

from custo_planilha import carregar_custos


def _criar_planilha(caminho, linhas):
    wb = Workbook()
    ws = wb.active
    ws.append(["codigo_pai", "descricao", "custo", "fonte", "conflito", "skus_do_grupo"])
    for linha in linhas:
        ws.append(linha)
    wb.save(caminho)


def test_expande_skus_do_grupo_para_dict_por_sku(tmp_path):
    caminho = tmp_path / "custo.xlsx"
    _criar_planilha(
        caminho,
        [["9128", "Tenis Via Marte", 73.49, "Tiny", "", "912835, 912836, 912837"]],
    )

    custos = carregar_custos(str(caminho))

    assert custos == {"912835": 73.49, "912836": 73.49, "912837": 73.49}


def test_linha_sem_custo_nao_entra_no_dict(tmp_path):
    caminho = tmp_path / "custo.xlsx"
    _criar_planilha(
        caminho,
        [["9999", "Sem custo cadastrado", "", "NENHUMA", "", "999901, 999902"]],
    )

    custos = carregar_custos(str(caminho))

    assert custos == {}
