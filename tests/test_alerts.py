import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

from alerts import atualizar_planilha, enviar_email, enviar_whatsapp, montar_mensagem_alerta
from margin_engine import ResultadoMargem


def _resultado_fake(custo_ausente=False):
    return ResultadoMargem(
        numero_pedido="3001",
        canal="shopee_1",
        receita=100.0,
        cmv=40.0,
        imposto=4.0,
        comissao=14.0,
        frete=0.0,
        ads=15.0,
        margem_contribuicao=-5.0,
        margem_pct=-5.0,
        custo_ausente=custo_ausente,
        skus_sem_custo=["SKU-SEM-CUSTO"] if custo_ausente else [],
    )


def test_planilha_cria_arquivo_com_cabecalho(tmp_path):
    caminho = tmp_path / "dashboard_margem.xlsx"
    atualizar_planilha(_resultado_fake(), str(caminho))

    wb = load_workbook(caminho)
    ws = wb.active
    assert ws.cell(row=1, column=1).value == "numero_pedido"
    assert ws.cell(row=2, column=1).value == "3001"


def test_planilha_acumula_linhas(tmp_path):
    caminho = tmp_path / "dashboard_margem.xlsx"
    atualizar_planilha(_resultado_fake(), str(caminho))
    atualizar_planilha(_resultado_fake(), str(caminho))

    wb = load_workbook(caminho)
    ws = wb.active
    assert ws.max_row == 3  # cabecalho + 2 linhas


def test_mensagem_alerta_flag_custo_ausente():
    mensagem = montar_mensagem_alerta(_resultado_fake(custo_ausente=True))
    assert "SKU-SEM-CUSTO" in mensagem
    assert "Custo ausente" in mensagem


def test_mensagem_alerta_sem_flag_quando_custo_ok():
    mensagem = montar_mensagem_alerta(_resultado_fake(custo_ausente=False))
    assert "Custo ausente" not in mensagem


def test_whatsapp_ativo_sem_credencial_nao_derruba_o_monitor(monkeypatch):
    monkeypatch.delenv("ZAPI_INSTANCE_ID", raising=False)
    monkeypatch.delenv("ZAPI_TOKEN", raising=False)
    config = {"alertas": {"whatsapp": {"ativo": True, "provider": "zapi", "destinatarios": ["5511999999999"]}}}

    enviar_whatsapp("mensagem de teste", config)  # nao deve levantar excecao


def test_callmebot_ativo_sem_apikey_do_numero_nao_derruba_o_monitor(monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY_5511999999999", raising=False)
    config = {"alertas": {"whatsapp": {"ativo": True, "provider": "callmebot", "destinatarios": ["5511999999999"]}}}

    enviar_whatsapp("mensagem de teste", config)  # nao deve levantar excecao


def test_callmebot_chama_api_com_apikey_do_numero_certo(monkeypatch):
    chamadas = []

    class _RespostaFake:
        def raise_for_status(self):
            pass

    def _get_fake(url, params, timeout):
        chamadas.append((url, params))
        return _RespostaFake()

    monkeypatch.setenv("CALLMEBOT_APIKEY_5511999999999", "123456")
    monkeypatch.setattr("alerts.requests.get", _get_fake)
    config = {"alertas": {"whatsapp": {"ativo": True, "provider": "callmebot", "destinatarios": ["5511999999999"]}}}

    enviar_whatsapp("mensagem de teste", config)

    assert len(chamadas) == 1
    _, params = chamadas[0]
    assert params == {"phone": "5511999999999", "text": "mensagem de teste", "apikey": "123456"}


def test_email_ativo_sem_credencial_nao_derruba_o_monitor(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    config = {"alertas": {"email": {"ativo": True, "destinatarios": ["financeiro@amooutlet.com.br"]}}}

    enviar_email("assunto", "corpo", config)  # nao deve levantar excecao
