-- Tabelas pra rodar o monitor em ambiente sem estado (GitHub Actions), sem
-- depender de SQLite local nem de arquivos de token locais. Colar no SQL
-- Editor do Supabase (rohkbgywmskllgxufmyp), alem dos 2 SQL ja rodados
-- antes (supabase_schema.sql e supabase_schema_custos.sql).
--
-- Diferente das tabelas de dashboard, estas NAO tem policy de leitura/
-- escrita pra chave anon - so a service role (usada pelo monitor.py /
-- monitor_cloud.py) acessa. Nao precisam disso, ninguem le do dashboard.

create table if not exists margin_monitor_checkpoint (
    conta_tiny text primary key,
    ultima_busca_iso text not null
);

create table if not exists margin_monitor_ml_tokens (
    conta text primary key,
    access_token text not null,
    refresh_token text not null,
    expires_in integer not null,
    obtido_em double precision not null
);

alter table margin_monitor_checkpoint enable row level security;
alter table margin_monitor_ml_tokens enable row level security;
-- Sem create policy nenhuma -> RLS habilitada mas sem policy = bloqueia
-- tudo pra quem nao for service role (que ignora RLS). Proposital.
