-- Numero do pedido DENTRO DO MARKETPLACE (ex.: numero da venda no Mercado
-- Livre) - diferente do numero_pedido, que e o identificador interno da
-- Tiny. Vem do campo generico pedido.numero_ecommerce (mesmo pra ML,
-- Shopee, Amazon, TikTok Shop - jah usado internamente pra achar a receita
-- exata via API do ML).

alter table public.margin_monitor_itens
  add column if not exists numero_ecommerce text;
