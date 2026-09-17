-- Tabelas do dashboard de margem (Grupo Amo / margin-monitor).
-- Colar no SQL Editor do Supabase do projeto rohkbgywmskllgxufmyp.
-- Escrita e feita pelo monitor.py via service role (ignora RLS).
-- Leitura e feita pelo dashboard via chave anon (RLS permissiva abaixo).

create table if not exists margin_monitor_pedidos (
    id bigint generated always as identity primary key,
    conta_tiny text not null,
    numero_pedido text not null,
    canal text not null,
    data_pedido text,
    receita numeric,
    margem_contribuicao numeric,
    margem_pct numeric,
    custo_ausente boolean not null default false,
    alertado boolean not null default false,
    processado_em timestamptz not null default now(),
    unique (conta_tiny, numero_pedido)
);

create table if not exists margin_monitor_itens (
    id bigint generated always as identity primary key,
    conta_tiny text not null,
    numero_pedido text not null,
    sku text not null,
    canal text not null,
    data_pedido text,
    quantidade numeric,
    receita numeric,
    cmv numeric,
    margem_contribuicao numeric,
    margem_pct numeric,
    custo_ausente boolean not null default false,
    processado_em timestamptz not null default now(),
    unique (conta_tiny, numero_pedido, sku)
);

create index if not exists idx_margin_monitor_pedidos_canal on margin_monitor_pedidos (canal);
create index if not exists idx_margin_monitor_pedidos_conta on margin_monitor_pedidos (conta_tiny);
create index if not exists idx_margin_monitor_itens_sku on margin_monitor_itens (sku);
create index if not exists idx_margin_monitor_itens_canal on margin_monitor_itens (canal);

alter table margin_monitor_pedidos enable row level security;
alter table margin_monitor_itens enable row level security;

-- Leitura liberada pra chave anon (o dashboard so faz SELECT). Escrita real
-- so acontece via service role (monitor.py), que ignora RLS de qualquer
-- forma - mesmo padrao ja usado no AmoCMV/Freelancer (RLS permissiva,
-- autorizacao de verdade fica na camada de aplicacao/acesso ao app).
create policy "leitura publica" on margin_monitor_pedidos for select using (true);
create policy "leitura publica" on margin_monitor_itens for select using (true);
