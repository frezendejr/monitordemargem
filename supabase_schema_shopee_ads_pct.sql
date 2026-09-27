-- % de Ads (TACOS) calculado dinamicamente pra cada loja Shopee, 1x ao dia
-- (gasto de Ads / faturamento bruto dos ultimos 29 dias, via API de Ads da
-- Shopee - ver shopee_ads_pct_job.py). shopee_pedidos.py le daqui a cada
-- ciclo em vez de usar o numero fixo do config.yaml.

create table if not exists margin_monitor_shopee_ads_pct (
    loja text primary key,
    ads_pct numeric not null,
    gasto_total numeric not null,
    faturamento_total numeric not null,
    calculado_em timestamptz not null default now()
);
