-- Codigo do anuncio no Mercado Livre (ex.: "MLB2048260121") por item vendido -
-- vem de order_items[].item.id na API do ML, so disponivel pros canais
-- Meli (receita_exata_via_ml). Fica NULL pros outros canais/itens antigos.

alter table public.margin_monitor_itens
  add column if not exists anuncio_id text;
