import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiny_client import TinyClient

# Fixtures baseadas em pedidos reais (--debug-pedido) das 4 contas do Grupo Amo.

CANAIS_AMOSHOES = {
    "meli_conta_2": {"tiny_identificador": "ML_AMOSHOESEIRELI 2"},
    "meli_conta_3": {"tiny_identificador": "ML_AMOSHOESEIRELI 3"},
    "shopee_1": {"tiny_identificador": "Shopee"},
    "tiktok_shop": {"tiny_identificador": "TikTok Shop"},
}


def _pedido_com_canal(nome_ecommerce: str, canal_venda: str | None = None) -> dict:
    ecommerce = {"id": "1", "numeroPedidoEcommerce": "X", "nomeEcommerce": nome_ecommerce}
    if canal_venda:
        ecommerce["canalVenda"] = canal_venda
    return {"numero": "72067", "ecommerce": ecommerce}


def test_identifica_meli_subconta_pelo_nome_completo():
    cliente = TinyClient(token="fake")
    pedido = _pedido_com_canal("ML_AMOSHOESEIRELI 2", canal_venda="Mercado Livre")
    assert cliente.identificar_canal(pedido, CANAIS_AMOSHOES) == "meli_conta_2"


def test_distingue_duas_contas_meli_na_mesma_conta_tiny():
    cliente = TinyClient(token="fake")
    pedido_2 = _pedido_com_canal("ML_AMOSHOESEIRELI 2", canal_venda="Mercado Livre")
    pedido_3 = _pedido_com_canal("ML_AMOSHOESEIRELI 3", canal_venda="Mercado Livre")

    assert cliente.identificar_canal(pedido_2, CANAIS_AMOSHOES) == "meli_conta_2"
    assert cliente.identificar_canal(pedido_3, CANAIS_AMOSHOES) == "meli_conta_3"


def test_identifica_shopee_e_tiktok_pelo_nome_generico():
    cliente = TinyClient(token="fake")
    assert cliente.identificar_canal(_pedido_com_canal("Shopee"), CANAIS_AMOSHOES) == "shopee_1"
    assert cliente.identificar_canal(_pedido_com_canal("TikTok Shop"), CANAIS_AMOSHOES) == "tiktok_shop"


def test_canal_desconhecido_retorna_none():
    cliente = TinyClient(token="fake")
    pedido = _pedido_com_canal("Canal Que Nao Existe No Config")
    assert cliente.identificar_canal(pedido, CANAIS_AMOSHOES) is None


def test_pedido_sem_bloco_ecommerce_retorna_none():
    cliente = TinyClient(token="fake")
    assert cliente.identificar_canal({"numero": "1"}, CANAIS_AMOSHOES) is None


def test_montar_itens_usa_id_produto_para_cache_de_custo_nao_codigo():
    # Bug real encontrado calibrando: produto.obter.php exige o id_produto
    # interno (nao o codigo/SKU) - custo_por_id_produto tem que ser buscado
    # por id_produto, senao custo_unitario vem sempre None.
    pedido_completo = {
        "itens": [
            {
                "item": {
                    "codigo": "912837",
                    "id_produto": "898908943",
                    "quantidade": "1.00",
                    "valor_unitario": "145.49",
                }
            }
        ]
    }
    custo_por_id_produto = {"898908943": 73.49}

    itens = TinyClient.montar_itens(pedido_completo, custo_por_id_produto)

    assert itens[0].sku == "912837"  # exibicao continua pelo codigo
    assert itens[0].custo_unitario == 73.49  # busca foi por id_produto
