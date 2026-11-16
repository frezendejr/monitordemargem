-- Snapshot (sempre o mais recente, nao historico) dos anuncios PAUSADOS de
-- cada marketplace - alimenta a aba "Anuncios pausados" do dashboard.
-- Escrita por anuncios_pausados_job.py (service role): Mercado Livre
-- (status paused, 4 contas) e Shopee (UNLIST, 2 lojas).

create table if not exists margin_monitor_anuncios_pausados (
    id bigint generated always as identity primary key,
    marketplace text not null check (marketplace in ('mercado_livre', 'shopee', 'amazon', 'tiktok')),
    conta text not null,
    anuncio_id text not null,
    titulo text,
    sku text,
    preco numeric,
    estoque int,
    status text,
    motivo text,
    link text,
    pausado_desde timestamptz,
    atualizado_em timestamptz not null default now(),
    unique (marketplace, conta, anuncio_id)
);

comment on table margin_monitor_anuncios_pausados is 'Snapshot mais recente dos anuncios pausados/deslistados por marketplace e conta';

create index if not exists idx_anuncios_pausados_marketplace on margin_monitor_anuncios_pausados (marketplace, conta);

alter table margin_monitor_anuncios_pausados enable row level security;

create policy "anuncios_pausados_select" on margin_monitor_anuncios_pausados for select to anon using (true);
