import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anuncios_pausados_job import linha_ml, linha_shopee

AGORA = "2026-11-16T12:00:00+00:00"


def test_linha_ml_traduz_motivo_e_pega_sku_do_atributo():
    anuncio = {
        "id": "MLB123",
        "title": "Tenis X",
        "price": 99.9,
        "available_quantity": 0,
        "status": "paused",
        "sub_status": ["out_of_stock"],
        "permalink": "https://produto.mercadolivre.com.br/MLB-123",
        "last_updated": "2026-11-10T10:00:00.000Z",
        "seller_custom_field": None,
        "attributes": [{"id": "SELLER_SKU", "value_name": "TX-38"}],
    }
    linha = linha_ml("meli_conta_1", anuncio, AGORA)
    assert linha["marketplace"] == "mercado_livre"
    assert linha["anuncio_id"] == "MLB123"
    assert linha["sku"] == "TX-38"
    assert linha["motivo"] == "Sem estoque"
    assert linha["estoque"] == 0


def test_linha_ml_sem_sub_status_e_pausa_manual():
    linha = linha_ml("meli_conta_2", {"id": "MLB9", "sub_status": []}, AGORA)
    assert linha["motivo"] == "Pausado pelo vendedor"
    assert linha["sku"] is None


def test_linha_shopee_extrai_preco_estoque_e_data():
    anuncio = {
        "item_id": 555,
        "item_name": "Sandalia Y",
        "item_sku": "SY-1",
        "item_status": "UNLIST",
        "price_info": [{"current_price": 49.9}],
        "stock_info_v2": {"summary_info": {"total_available_stock": 12}},
        "update_time": 1763290000,
    }
    linha = linha_shopee("shopee_1", anuncio, AGORA)
    assert linha["anuncio_id"] == "555"
    assert linha["preco"] == 49.9
    assert linha["estoque"] == 12
    assert linha["motivo"] == "Deslistado"
    assert linha["pausado_desde"].startswith("2025-11-16")


def test_linha_shopee_com_variacoes_sem_price_info():
    linha = linha_shopee("shopee_2", {"item_id": 1, "item_sku": ""}, AGORA)
    assert linha["preco"] is None
    assert linha["sku"] is None
    assert linha["pausado_desde"] is None
