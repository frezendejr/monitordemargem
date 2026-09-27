-- Snapshot (sempre o mais recente, nao historico) de performance de Ads por
-- campanha, ultimos 7 dias - alimenta a aba "Monitor de Ads" do dashboard.
-- Recalculado 1x ao dia (ver ads_monitor_job.py) pras 2 lojas Shopee e as
-- 4 contas Mercado Livre.

create table if not exists margin_monitor_ads_campanhas (
    id bigint generated always as identity primary key,
    marketplace text not null check (marketplace in ('mercado_livre', 'shopee')),
    conta text not null,
    campanha_id text not null,
    campanha_nome text,
    dias_periodo int not null default 7,
    gasto numeric not null default 0,
    faturamento numeric not null default 0,
    roas numeric,
    tacos numeric,
    atualizado_em timestamptz not null default now(),
    unique (marketplace, conta, campanha_id)
);

comment on table margin_monitor_ads_campanhas is 'Snapshot mais recente (nao historico) de gasto/faturamento/ROAS/TACOS por campanha de Ads, ultimos 7 dias - Mercado Livre e Shopee';

create index if not exists idx_ads_campanhas_marketplace on margin_monitor_ads_campanhas (marketplace);

alter table margin_monitor_ads_campanhas enable row level security;

create policy "ads_campanhas_select" on margin_monitor_ads_campanhas for select to anon using (true);
