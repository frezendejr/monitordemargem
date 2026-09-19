-- Evolui margin_monitor_metas de "1 meta total por mes" pra "1 meta por
-- (mes, canal)" - a meta por canal e uma decisao estrategica do time (ex.:
-- "crescer Amazon, manter Shoppe 1"), nao um numero que da pra calcular
-- olhando pra tras. Tambem adiciona a "Hiper Meta" (segundo nivel, mais
-- agressivo, que a planilha de referencia do time ja usa).
--
-- Registros antigos (so total, sem canal) sao removidos - a tela nova pede
-- meta por canal, entao precisam ser recadastrados.

alter table public.margin_monitor_metas
  add column if not exists canal text,
  add column if not exists hiper_meta_faturamento numeric;

alter table public.margin_monitor_metas drop constraint if exists margin_monitor_metas_pkey;
delete from public.margin_monitor_metas where canal is null;
alter table public.margin_monitor_metas alter column canal set not null;
alter table public.margin_monitor_metas add primary key (mes, canal);
