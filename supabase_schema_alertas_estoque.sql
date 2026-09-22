-- Alertas de estoque/Ads em anuncios do Mercado Livre (grade furada com
-- ADS ativo, estoque zerado) - so o backend (service role, via
-- estoque_ads.py) escreve; o dashboard le pra mostrar o alerta piscando
-- (mesmo padrao de leitura publica das outras tabelas de apoio).
--
-- unique(anuncio_id, tipo) permite upsert: mesmo alerta so notifica 1 vez
-- (fica "ativo" ate a condicao deixar de ser verdadeira, quando
-- estoque_ads.py marca ativo=false / resolvido_em).
--
-- Colar no SQL Editor do Supabase (rohkbgywmskllgxufmyp).

create table if not exists margin_monitor_alertas_estoque (
    id bigint generated always as identity primary key,
    conta_tiny text not null,
    canal text not null,
    anuncio_id text not null,
    tipo text not null, -- 'grade_furada' | 'estoque_zerado'
    detalhe text,
    ativo boolean not null default true,
    criado_em timestamptz not null default now(),
    resolvido_em timestamptz,
    unique (anuncio_id, tipo)
);

alter table margin_monitor_alertas_estoque enable row level security;

create policy "anon pode ler alertas de estoque"
  on margin_monitor_alertas_estoque
  for select
  to anon
  using (true);
