-- Categoria de produto (Calcados, Utensilios domesticos, Fitness, ...) -
-- resolvida automaticamente pela API do Mercado Livre (category_id do
-- anuncio -> nome da categoria), 1 linha por codigo_pai (grupo de
-- variacoes/tamanhos). So o backend (service role, via monitor.py/
-- monitor_cloud.py) escreve aqui - o dashboard so le (chave anon).
--
-- Colar no SQL Editor do Supabase (rohkbgywmskllgxufmyp).

create table if not exists margin_monitor_categorias (
    codigo_pai text primary key,
    categoria text not null,
    category_id_ml text,
    atualizado_em timestamptz not null default now()
);

alter table margin_monitor_categorias enable row level security;

create policy "anon pode ler categorias"
  on margin_monitor_categorias
  for select
  to anon
  using (true);
