-- Meta mensal de faturamento/margem, preenchida pelo time direto no
-- dashboard (aba "Visão geral"). O dashboard so grava o TOTAL do mes - a
-- divisao por dia (sazonalidade por dia da semana) e por conta/canal e
-- calculada em tempo real a partir do historico, nao fica gravada aqui.
--
-- RLS permissiva igual a margin_monitor_custos: e so um numero-alvo de
-- negocio, baixo risco, e o dashboard publico so tem a chave anon.

create table if not exists public.margin_monitor_metas (
  mes date primary key,  -- sempre o dia 1 do mes, ex: 2026-10-01
  meta_faturamento numeric not null,
  meta_margem_pct numeric not null,
  atualizado_em timestamptz not null default now()
);

alter table public.margin_monitor_metas enable row level security;

create policy "metas_select_anon" on public.margin_monitor_metas
  for select using (true);

create policy "metas_insert_anon" on public.margin_monitor_metas
  for insert with check (true);

create policy "metas_update_anon" on public.margin_monitor_metas
  for update using (true);
