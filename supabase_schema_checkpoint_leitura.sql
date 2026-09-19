-- Libera LEITURA (so select, nao insert/update/delete) da tabela de
-- checkpoint pra chave anon - usada pelo dashboard.py pra mostrar "ultima
-- sincronizacao" e alertar quando o monitor estiver atrasado.
--
-- margin_monitor_ml_tokens continua SEM NENHUMA policy (guarda token OAuth
-- do Mercado Livre - nunca deve virar legivel pela chave anon). So mexe em
-- margin_monitor_checkpoint, que so tem conta_tiny + timestamp.
--
-- Colar no SQL Editor do Supabase (rohkbgywmskllgxufmyp).

create policy "anon pode ler checkpoint"
  on margin_monitor_checkpoint
  for select
  to anon
  using (true);
