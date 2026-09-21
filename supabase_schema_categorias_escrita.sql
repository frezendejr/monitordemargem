-- Libera ESCRITA (insert/update) da tabela de categorias pra chave anon,
-- complementando supabase_schema_categorias.sql (que so tinha leitura) -
-- mesmo padrao/risco aceito ja usado em margin_monitor_custos (so
-- classificacao de produto, nao dado financeiro critico, sempre da pra
-- corrigir depois). Isso permite o time classificar manualmente no
-- dashboard um codigo_pai que a API do Meli ainda nao classificou (produto
-- que so vendeu por Shopee/Amazon/TikTok Shop, por exemplo).
--
-- Colar no SQL Editor do Supabase (rohkbgywmskllgxufmyp).

create policy "escrita publica categorias" on margin_monitor_categorias for insert with check (true);
create policy "atualizacao publica categorias" on margin_monitor_categorias for update using (true) with check (true);
