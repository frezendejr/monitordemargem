import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage
from margin_engine import ResultadoMargem


def _resultado_fake(numero="2001"):
    return ResultadoMargem(
        numero_pedido=numero,
        canal="meli_conta_1",
        receita=100.0,
        cmv=40.0,
        imposto=4.0,
        comissao=12.0,
        frete=15.0,
        ads=0.0,
        margem_contribuicao=29.0,
        margem_pct=29.0,
        custo_ausente=False,
        skus_sem_custo=[],
    )


def test_dedupe_pedido_nao_processado_duas_vezes(tmp_path):
    db_path = str(tmp_path / "teste.db")
    with storage.sessao(db_path) as conn:
        assert storage.ja_processado(conn, "conta_amoshoes", "2001") is False

        storage.salvar_pedido(conn, "conta_amoshoes", _resultado_fake("2001"), alertado=False)
        assert storage.ja_processado(conn, "conta_amoshoes", "2001") is True
        assert storage.ja_processado(conn, "conta_amoshoes", "2002") is False


def test_mesmo_numero_pedido_em_contas_diferentes_nao_colide(tmp_path):
    db_path = str(tmp_path / "teste.db")
    with storage.sessao(db_path) as conn:
        storage.salvar_pedido(conn, "conta_amoshoes", _resultado_fake("1001"), alertado=False)

        # "1001" na conta_inamorato e um pedido diferente, nao deve ser
        # considerado processado so porque "1001" ja existe na outra conta.
        assert storage.ja_processado(conn, "conta_inamorato", "1001") is False


def test_checkpoint_e_por_conta_e_persiste_entre_sessoes(tmp_path):
    db_path = str(tmp_path / "teste.db")
    with storage.sessao(db_path) as conn:
        assert storage.obter_checkpoint(conn, "conta_amoshoes") is None
        storage.salvar_checkpoint(conn, "conta_amoshoes", "2026-01-01T00:00:00+00:00")
        storage.salvar_checkpoint(conn, "conta_inamorato", "2026-02-01T00:00:00+00:00")

    with storage.sessao(db_path) as conn:
        assert storage.obter_checkpoint(conn, "conta_amoshoes") == "2026-01-01T00:00:00+00:00"
        assert storage.obter_checkpoint(conn, "conta_inamorato") == "2026-02-01T00:00:00+00:00"
