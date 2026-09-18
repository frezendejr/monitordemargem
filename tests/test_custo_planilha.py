import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook

from custo_planilha import carregar_custos, sobrepor_custos_do_dashboard


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


def test_sobrepor_custos_sem_credencial_supabase_nao_quebra(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    custos = sobrepor_custos_do_dashboard({"912837": 73.49})

    assert custos == {"912837": 73.49}  # nao mudou, so nao quebrou


def test_sobrepor_custos_do_dashboard_tem_prioridade(monkeypatch):
    class _RespostaFake:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"sku": "999999", "custo": 42.0}, {"sku": "912837", "custo": 80.0}]

    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "chave-fake")
    monkeypatch.setattr("custo_planilha.requests.get", lambda *a, **k: _RespostaFake())

    custos = sobrepor_custos_do_dashboard({"912837": 73.49})

    assert custos == {"912837": 80.0, "999999": 42.0}  # dashboard sobrescreveu 912837 e adicionou 999999
