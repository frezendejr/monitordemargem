"""Persistencia local: dedupe de pedidos ja processados e checkpoint de
'ultima atualizacao buscada', em SQLite (margem_monitor.db).

O Grupo Amo tem 4 contas Tiny distintas (uma por CNPJ), cada uma com seu
proprio token e seus proprios canais de venda dentro dela. Numero de pedido
so e unico DENTRO de uma conta Tiny - duas contas podem ter um pedido "1001"
cada uma sem relacao entre si - por isso toda chave aqui e (conta, numero).
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH_PADRAO = "margem_monitor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pedidos_processados (
    conta_tiny TEXT NOT NULL,
    numero_pedido TEXT NOT NULL,
    canal TEXT NOT NULL,
    data_pedido TEXT,
    receita REAL,
    margem_contribuicao REAL,
    margem_pct REAL,
    custo_ausente INTEGER NOT NULL DEFAULT 0,
    alertado INTEGER NOT NULL DEFAULT 0,
    processado_em TEXT NOT NULL,
    PRIMARY KEY (conta_tiny, numero_pedido)
);

CREATE TABLE IF NOT EXISTS checkpoint (
    conta_tiny TEXT PRIMARY KEY,
    ultima_busca_iso TEXT NOT NULL
);
"""


def conectar(db_path: str = DB_PATH_PADRAO) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


@contextmanager
def sessao(db_path: str = DB_PATH_PADRAO):
    conn = conectar(db_path)
    try:
        yield conn
    finally:
        conn.close()


def ja_processado(conn: sqlite3.Connection, conta_tiny: str, numero_pedido: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM pedidos_processados WHERE conta_tiny = ? AND numero_pedido = ?",
        (conta_tiny, numero_pedido),
    )
    return cur.fetchone() is not None


def salvar_pedido(conn: sqlite3.Connection, conta_tiny: str, resultado, alertado: bool) -> None:
    conn.execute(
        """
        INSERT INTO pedidos_processados
            (conta_tiny, numero_pedido, canal, data_pedido, receita,
             margem_contribuicao, margem_pct, custo_ausente, alertado, processado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conta_tiny, numero_pedido) DO UPDATE SET
            margem_contribuicao = excluded.margem_contribuicao,
            margem_pct = excluded.margem_pct,
            custo_ausente = excluded.custo_ausente,
            alertado = excluded.alertado
        """,
        (
            conta_tiny,
            resultado.numero_pedido,
            resultado.canal,
            getattr(resultado, "data_pedido", None),
            resultado.receita,
            resultado.margem_contribuicao,
            resultado.margem_pct,
            int(resultado.custo_ausente),
            int(alertado),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def obter_checkpoint(conn: sqlite3.Connection, conta_tiny: str) -> str | None:
    cur = conn.execute(
        "SELECT ultima_busca_iso FROM checkpoint WHERE conta_tiny = ?",
        (conta_tiny,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def salvar_checkpoint(conn: sqlite3.Connection, conta_tiny: str, quando_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO checkpoint (conta_tiny, ultima_busca_iso) VALUES (?, ?)
        ON CONFLICT(conta_tiny) DO UPDATE SET ultima_busca_iso = excluded.ultima_busca_iso
        """,
        (conta_tiny, quando_iso),
    )
    conn.commit()
