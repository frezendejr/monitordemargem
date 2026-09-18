-- Tabela de custos preenchidos manualmente pelo dashboard (SKUs que nao
-- tinham custo em nenhuma fonte da planilha original). Colar no SQL Editor
-- do Supabase (rohkbgywmskllgxufmyp), alem do supabase_schema.sql ja
-- rodado antes - esse aqui e so um complemento.
--
-- Diferente de margin_monitor_pedidos/itens, essa tabela permite ESCRITA
-- pela chave anon tambem (nao so leitura) - e proposital: o dashboard so
-- tem a chave anon, e precisa gravar o custo que o time digitar nele. O
-- risco (qualquer um com o link do dashboard pode editar custo) e aceitavel
-- aqui porque e so custo de produto, nao dado financeiro critico, e sempre
-- da pra corrigir depois. Se quiser mais controle, restrinja quem acessa o
-- link do dashboard nas configuracoes do app no Streamlit Cloud.

create table if not exists margin_monitor_custos (
    sku text primary key,
    custo numeric not null,
    atualizado_em timestamptz not null default now()
);

alter table margin_monitor_custos enable row level security;

create policy "leitura publica" on margin_monitor_custos for select using (true);
create policy "escrita publica" on margin_monitor_custos for insert with check (true);
create policy "atualizacao publica" on margin_monitor_custos for update using (true) with check (true);
