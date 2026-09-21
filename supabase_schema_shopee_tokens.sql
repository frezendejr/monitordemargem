-- Tokens OAuth da Shopee (Open Platform v2) - mesmo padrao de
-- margin_monitor_ml_tokens (supabase_schema_cloud.sql): usado so quando o
-- monitor roda em ambiente sem disco persistente (GitHub Actions,
-- MARGIN_MONITOR_CLOUD=1), senao o refresh_token se perde entre execucoes.
--
-- Sem NENHUMA policy de proposito (RLS habilitada, sem policy = bloqueia
-- tudo pra quem nao for service role) - guarda token de acesso, nunca pode
-- ser legivel pela chave anon do dashboard.
--
-- Colar no SQL Editor do Supabase (rohkbgywmskllgxufmyp).

create table if not exists margin_monitor_shopee_tokens (
    loja text primary key,
    shop_id text not null,
    access_token text not null,
    refresh_token text not null,
    expire_in integer,
    obtido_em double precision not null
);

alter table margin_monitor_shopee_tokens enable row level security;
