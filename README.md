# Monitor de Margem de Contribuicao (ML + Shopee + Amazon + TikTok Shop)

Monitor quase-tempo-real da margem de contribuicao das vendas do Grupo Amo /
Amo Outlet, lendo os pedidos direto do Tiny ERP (que ja centraliza os
canais). Alerta a equipe por WhatsApp e e-mail sempre que um pedido sai com
margem negativa, mantem um historico em `dashboard_margem.xlsx` e usa um
SQLite (`margem_monitor.db`) para nao alertar o mesmo pedido duas vezes.

**Custo do produto vem de uma planilha de apoio (`custo_por_codigo_pai.xlsx`),
nao mais de consulta ao vivo no Tiny** - ver secao "Custo dos produtos"
abaixo. Motivo: o custo cadastrado no Tiny estava zerado/ausente numa fatia
grande do catalogo.

**Multi-conta:** o Grupo Amo tem 4 contas Tiny distintas, uma por CNPJ, cada
uma com seu proprio token de API. Varios canais de venda podem conviver
dentro da MESMA conta Tiny (ex.: a conta `amoshoes` sozinha reune Meli Conta
2, Meli Conta 3, Shopee 1 e TikTok Shop). O monitor roda um ciclo em cada
conta a cada iteracao - ver `tiny_contas` em `config.yaml`.

| Conta Tiny (chave)         | CNPJ / apelido           | Canais dentro dela                          |
|-----------------------------|--------------------------|----------------------------------------------|
| `conta_amoshoesgyn`         | amoshoesgyn              | Meli Conta 1                                  |
| `conta_amoshoes`             | amoshoes                 | Meli Conta 2, Meli Conta 3, Shopee 1, TikTok Shop |
| `conta_inamorato`            | inamorato                | Meli Conta 4, Amazon                          |
| `conta_28849110000141`       | CNPJ 28.849.110/0001-41  | Shopee 2                                      |

Formula: `MC = Receita - CMV - Imposto - Comissao/tarifa do canal - Frete -
% de ads estimado`.

**Importante: Imposto, Comissao, Frete e Ads sao sempre estimativas em % (do
`config.yaml`), nunca os valores exatos do pedido.** Confirmado contra um
pedido real (ver "Avisos de campos ajustados" no fim deste arquivo): o Tiny
nao expoe a tarifa de venda real, o frete real cobrado do vendedor nem
estornos de cupom/bonus - esses numeros so existem no extrato de cada
marketplace, que este projeto nao integra. O monitor e para alertar rapido
quando algo sai muito no vermelho, nao para reconciliar centavo a centavo.

## Status

Calibrado e rodado de ponta a ponta contra as 4 contas Tiny reais (3 vezes,
com bugs reais corrigidos no processo - ver "Avisos de campos ajustados").
As 4 contas de Mercado Livre estao autorizadas via OAuth e usam receita
liquida EXATA (nao estimada) via `ml_client.py`. WhatsApp via CallMeBot
configurado e testado. Dashboard (Streamlit + Supabase) construido, falta
rodar o SQL no Supabase e publicar. **Falta**: preencher os percentuais
reais de Amazon e TikTok Shop (nao estavam no escopo original), e uma rodada
de validacao final agora
que os bugs de custo foram corrigidos (a ultima rodada completa ainda tinha
o bug do cache de custo).

## Dashboard (margem venda a venda, por marketplace/conta/produto)

`dashboard.py` (Streamlit) mostra indicadores de margem filtrando por conta
Tiny, canal (marketplace/subconta), com tabela venda a venda e ranking de
produtos por margem (melhores/piores SKUs). Le do **Supabase**, nao do
SQLite local - o SQLite (`margem_monitor.db`) continua sendo so o controle
operacional de dedupe/checkpoint do `monitor.py`.

### 1. Rodar o SQL no Supabase (uma vez)

Colar [`supabase_schema.sql`](supabase_schema.sql) no SQL Editor do projeto
`rohkbgywmskllgxufmyp` (o mesmo do AmoApp) - cria `margin_monitor_pedidos` e
`margin_monitor_itens`, com RLS permissiva pra leitura (a escrita real e
sempre via service role, que ignora RLS).

### 2. Preencher `.env` com as credenciais do Supabase

```
SUPABASE_URL=https://rohkbgywmskllgxufmyp.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...   # monitor.py escreve com essa
SUPABASE_ANON_KEY=...           # dashboard.py so le com essa
```

Essas 3 chaves ja existem no `.env.local` do projeto AmoApp (pasta
`Claude Code`) - copiar de la, nao gerar credencial nova.

### 3. `monitor.py` passa a gravar no Supabase automaticamente

Cada pedido processado grava no SQLite local (como sempre) **e** no
Supabase (`supabase_writer.py`) - o pedido inteiro em
`margin_monitor_pedidos` e o rateio por item (SKU) em
`margin_monitor_itens`. Se a gravacao no Supabase falhar (rede, credencial
faltando), so loga e segue - nunca derruba o monitor, ja que o SQLite local
e a fonte de verdade operacional.

**Importante**: como o rateio por item so existe a partir dessa versao,
pedidos processados ANTES dela nao tem linha em `margin_monitor_itens` -
o dashboard "por produto" so cobre pedidos novos.

### 4. Rodar o dashboard local

```bash
streamlit run dashboard.py
```

### 5. Publicar (Streamlit Community Cloud, gratuito)

1. Acesse [share.streamlit.io](https://share.streamlit.io), logue com a
   conta GitHub que tem acesso ao repo `monitordemargem`.
2. "New app" -> escolher o repo, branch `master`, arquivo `dashboard.py`.
3. Em "Advanced settings" -> "Secrets", colar (formato TOML, nao `.env`):
   ```toml
   SUPABASE_URL = "https://rohkbgywmskllgxufmyp.supabase.co"
   SUPABASE_ANON_KEY = "..."
   ```
   **Nunca colar a `SUPABASE_SERVICE_ROLE_KEY` aqui** - o dashboard so
   precisa ler, e a service role da acesso de escrita irrestrito.
4. Deploy. O link gerado (`*.streamlit.app`) pode ser compartilhado com o
   time - o repo pode continuar privado, o Streamlit Cloud so precisa de
   acesso de leitura a ele (via OAuth do GitHub).

## Custo dos produtos (planilha de apoio)

O Tiny (mesmo depois de corrigido o bug de parametro `id`/`codigo` - ver
"Avisos de campos ajustados") continua tendo custo zerado/ausente numa fatia
grande do catalogo (~24% dos codigos pai numa amostra real). Em vez de
depender so dele, o custo usado no calculo de margem vem de uma **planilha
de apoio mantida por fora**, `custo_por_codigo_pai.xlsx`, uma linha por
"codigo pai" (agrupa as variacoes de tamanho/cor de um mesmo modelo) com o
custo consolidado.

**Gerar/atualizar a planilha** a partir de 2 exports (`gerar_custos_por_codigo_pai.py`):
1. Export do cadastro do Tiny com a aba `BaseDados` (traz `Codigo (SKU)`,
   `Preco de custo` e `Codigo do pai`).
2. Export de publicacoes do Mercado Livre (traz `SKU` e `Custo do produto
   carregado`, o custo cadastrado direto no ML).

```bash
python gerar_custos_por_codigo_pai.py --base-dados "<export do Tiny>.xlsx" --custo-ml "<export do ML>.xlsx"
```

O script agrupa por codigo pai e usa qualquer custo nao-zero encontrado em
qualquer SKU do grupo (Tiny ou ML), sinalizando na planilha de saida:
- **linha com custo em branco** - nenhuma das 2 fontes tinha custo pra
  nenhum SKU daquele grupo. Precisa preencher a mao.
- **coluna `conflito` = SIM** - SKUs do mesmo grupo tem custos diferentes
  cadastrados entre si (pode ser erro de cadastro ou custo que mudou).

Depois de corrigir a mao os que faltam, o `monitor.py` le
`custo_por_codigo_pai.xlsx` (`custo_planilha.py`) **a cada ciclo** (nao so
na inicializacao), entao uma correcao na planilha vale a partir do proximo
ciclo, sem precisar reiniciar o processo. Um SKU que nao aparece na planilha
vira `custo_ausente` no pedido, do mesmo jeito que antes.

## Integracao com Mercado Livre (receita exata)

Pra pedidos de Mercado Livre, alem do Tiny, o monitor consulta a API do
proprio ML + Mercado Pago (`ml_client.py`) pra pegar a receita liquida EXATA
do pedido (`transaction_details.net_received_amount`) - ja descontando
tarifa/frete real e considerando cupom reembolsado pelo ML, sem precisar
estimar por `%`. Confirmado batendo 100% com o extrato real de um pedido
(pedido 900314112: API disse R$115,45, extrato do ML mostrou R$115,45).

Cada uma das 4 contas Meli tem sua **propria aplicacao OAuth** (Client
ID/Client Secret proprios, nao compartilhados) e seu proprio arquivo de
tokens (`ml_tokens_meli_conta_N.json`, gitignored). Fluxo de autorizacao,
uma vez por conta (repetir quando o refresh_token expirar, ~6 meses depois):

```bash
python ml_client.py --conta meli_conta_1 --auth-url
# abrir a URL, logar como ADMIN da conta Meli 1, autorizar
# copiar o "code" da URL de retorno (repara: caiu em .../login?code=...
# porque o middleware do AmoApp intercepta a rota /oauth/ml-callback que
# nao existe - o code continua la, so muda o path)
python ml_client.py --conta meli_conta_1 --exchange-code SEU_CODE
```

Repetir pra `meli_conta_2`, `meli_conta_3` e `meli_conta_4`.

**Limitacao encontrada**: a API de pedidos do ML (`/orders/{id}`) so parece
servir pedidos BEM recentes - pedidos de algumas horas atras ja devolvem 404
"Order do not exists", enquanto pedidos de poucos minutos atras funcionam.
Isso e esperado no uso real (o monitor roda perto do tempo real, entao os
pedidos que ele processa sao sempre recentes), mas dificulta testar contra
pedidos antigos na calibracao. Por isso o canal so usa a receita exata
`receita_exata_via_ml: true` **com fallback automatico e seguro** pra
`comissao_pct`/`frete_pct` estimados quando a API do ML falhar por qualquer
motivo (pedido antigo, token expirado, rate limit) - ver
`_resolver_receita_e_config` em `monitor.py`.

## Rodando na nuvem (GitHub Actions, sem depender do PC)

`monitor.py` (local, loop continuo ou `--once`) depende de um PC ligado. Pra
rodar sem isso, `monitor_cloud.py` e uma versao stateless (sem SQLite, sem
loop - um ciclo so por execucao) feita pra rodar via
`.github/workflows/monitor.yml` (cron a cada 15 min + `workflow_dispatch`
pra disparar manual pela aba Actions do GitHub).

**Diferencas do modo nuvem:**
- Dedupe/checkpoint: `storage_supabase.py` em vez de `storage.py` (SQLite) -
  usa a propria `margin_monitor_pedidos` como fonte de dedupe (se o pedido
  ja esta la, ja foi processado) + uma tabela `margin_monitor_checkpoint`
  nova, so pra isso.
- Custo: direto do Supabase (`sobrepor_custos_do_dashboard({})`, sem
  planilha local) - por isso a planilha `custo_por_codigo_pai.xlsx` precisa
  ter sido migrada pro Supabase uma vez (rodado manualmente, ver script
  inline usado na migracao - nao ha um comando dedicado ainda, foi feito
  direto).
- Token do Mercado Livre: com `MARGIN_MONITOR_CLOUD=1` setado, `ml_client.py`
  persiste o access_token/refresh_token na tabela `margin_monitor_ml_tokens`
  do Supabase em vez de arquivo local `ml_tokens_*.json` - **essencial**,
  porque o refresh_token e de uso unico e um runner do GitHub Actions nao
  tem disco persistente entre execucoes (perderia o token depois do 1o
  ciclo sem isso).
- Nao escreve `dashboard_margem.xlsx` (nao faz sentido num runner efemero).

**Setup (uma vez):**

1. Rodar `supabase_schema_cloud.sql` no Supabase (cria
   `margin_monitor_checkpoint` e `margin_monitor_ml_tokens`, sem RLS pra
   chave anon - so service role acessa).
2. Configurar os **Secrets** do repositorio no GitHub (Settings → Secrets
   and variables → Actions → New repository secret), um por variavel usada
   no workflow: os `TINY_API_TOKEN_*`, `ML_APP_ID_*`/`ML_CLIENT_SECRET_*`,
   `ML_REDIRECT_URI`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `CALLMEBOT_APIKEY_*` (ou credenciais de outro provider de WhatsApp),
   `SMTP_*` (se for usar e-mail), e **`CONFIG_YAML`** (o conteudo INTEIRO do
   `config.yaml` colado como um unico secret - o workflow escreve isso num
   arquivo `config.yaml` no runner antes de rodar).
3. Testar disparando manual: aba **Actions** do repositorio no GitHub →
   "Monitor de margem (ciclo agendado)" → **"Run workflow"**.

Depois de confirmar que funciona, o cron cuida do resto sozinho - nao
precisa de PC ligado, nem de tarefa agendada do Windows.

## Arquivos

- `monitor.py` - loop principal LOCAL (`--once` roda uma vez, sem argumento
  roda em loop continuo). `--custo-planilha` sobrescreve o caminho da
  planilha de custo (padrao `custo_por_codigo_pai.xlsx`).
- `monitor_cloud.py` - versao stateless pra rodar no GitHub Actions (sem
  SQLite, sem loop - ver secao "Rodando na nuvem"). `storage_supabase.py` -
  dedupe/checkpoint via Supabase, usado so por ele.
- `tiny_client.py` - cliente da API do Tiny (pedidos + identificacao de
  canal). `obter_produto`/`obter_custo_produto`/`--debug-produto` ainda
  existem pra depuracao manual, mas o `monitor.py` nao usa mais isso pra
  calcular margem - custo vem da planilha (ver abaixo).
- `custo_planilha.py` - le `custo_por_codigo_pai.xlsx` e devolve
  `{sku: custo}`. `gerar_custos_por_codigo_pai.py` - gera essa planilha a
  partir de 2 exports (Tiny + Mercado Livre). Ver secao "Custo dos
  produtos".
- `margin_engine.py` - calculo da margem por pedido, incluindo o rateio por
  item (`ItemMargem`) usado pelo dashboard "por produto". Testado, so
  deveria mudar se a formula mudar.
- `storage.py` - SQLite para dedupe e checkpoint de "ultima busca" (controle
  operacional, nao alimenta o dashboard).
- `supabase_writer.py` - grava cada pedido/item calculado no Supabase, so
  pra alimentar o `dashboard.py`. `supabase_schema.sql` - SQL das tabelas
  (colar no SQL Editor do Supabase, ver secao "Dashboard").
- `dashboard.py` - dashboard Streamlit (margem por marketplace/conta/
  produto/venda), le do Supabase. Ver secao "Dashboard".
- `alerts.py` - WhatsApp (CallMeBot, Z-API ou Meta Cloud API), e-mail (SMTP)
  e atualizacao da planilha de historico (`dashboard_margem.xlsx` - nao
  confundir com a planilha de custo).
- `ml_client.py` - cliente OAuth2 do Mercado Livre + Mercado Pago, usado
  pelos canais Meli pra pegar a receita liquida EXATA do pedido (ver secao
  "Integracao com Mercado Livre" abaixo). `ml_tokens_meli_conta_N.json`
  guarda os tokens de cada conta (gitignored).
- `config.example.yaml` / `config.yaml` - 4 contas Tiny (`tiny_contas`), cada
  uma com seu `token_env` e a lista de canais dentro dela: imposto, % de ads,
  comissao/frete (fixo ou por faixa de valor do pedido), e
  `receita_exata_via_ml: true` nos canais Meli.
- `.env.example` / `.env` - um `TINY_API_TOKEN_*` por conta Tiny, um
  `ML_APP_ID_*`/`ML_CLIENT_SECRET_*` por conta Meli, + credenciais de
  WhatsApp/e-mail. **Nunca commitar o `.env` real.**

## Instalacao

```bash
cd margin-monitor
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # depois preencher com credenciais reais
```

`config.yaml` ja existe (copia de `config.example.yaml`) - editar com os
numeros reais do negocio antes de rodar contra o Tiny de verdade.

## Rodando os testes

```bash
python -m pytest
```

## Calibrando contra o Tiny real (nesta ordem)

1. ~~Confirmar a versao da API do Tiny.~~ **Feito**: e v2 classica (token
   unico), como o codigo ja assumia.

2. ~~Rodar os comandos de depuracao com um pedido real, um por conta Tiny.~~
   **Feito** para as 4 contas, incluindo `tiny_identificador` de Meli Conta 3
   (`"Mercado Livre 3"`) e Meli Conta 4 (`"Mercado Livre 4"`) - os palpites
   originais no padrao `"ML_<NOME> <numero>"` estavam ERRADOS pra essas duas
   (só a 1 e a 2 seguem esse padrao). `--debug-produto` tambem confirmado
   (ver bug do parametro `id` x `codigo` nos avisos abaixo).

3. ~~Confirmar o parametro de busca por atualizacao.~~ **Feito**:
   `dataAtualizacao` em `pedidos.pesquisa.php` funciona e o resumo ja traz
   `id` + `numero` juntos. Testado com:
   ```bash
   python tiny_client.py --conta conta_amoshoes --debug-busca 3
   ```

4. **Preencher os percentuais que ainda faltam em `config.yaml`**:
   `imposto_pct`/`ads_pct`/`comissao_pct`/`frete_pct` da **Amazon** e do
   **TikTok Shop** (nao estavam no escopo original, que so cobria ML+Shopee -
   marcados `# PREENCHER`). As 4 contas Meli ja usam receita exata via ML,
   entao `comissao_pct`/`frete_pct` delas so importam como fallback.

5. ~~Preencher o `.env`~~. **Feito**: os 4 tokens do Tiny + os 4 pares
   `ML_APP_ID_*`/`ML_CLIENT_SECRET_*` (um app OAuth por conta Meli) + as 4
   autorizacoes OAuth completas. Falta Z-API/Meta (WhatsApp) e SMTP (e-mail)
   quando for testar os alertas.

6. ~~Rodar `python monitor.py --once`~~ **Feito 3x**, com 2 bugs reais
   corrigidos no processo (ver avisos abaixo). A ultima rodada completa
   ainda tinha o bug do cache de custo - rodar mais uma vez pra validar os
   numeros finais antes de confiar neles pra valer.

7. **Testar o alerta de margem negativa** de ponta a ponta contra um pedido
   real conhecido (ex.: item abaixo de R$ 79 no ML).

8. **Colocar para rodar continuamente** (cron chamando `monitor.py --once` a
   cada poucos minutos, ou um servico com `monitor.py` sem argumento).

## Coisas para NAO fazer

- Nao reduzir `intervalo_minutos` para menos de alguns minutos sem checar o
  limite de requisicoes da API do Tiny. **Confirmado na pratica**: uma
  primeira rodada cobrindo 24h de historico bateu no rate limit do Tiny
  ("API Bloqueada") numa das contas, e o bloqueio durou mais que "alguns
  minutos" (persistiu por pelo menos ~15 min entre duas tentativas). Rodar
  com o intervalo curto pretendido (poucos minutos, poucos pedidos novos por
  vez) deve evitar isso - o problema so apareceu no backfill inicial grande.
- Nao tratar `custo_ausente` como bug a esconder - e proposital: SKUs com
  custo zero no Tiny sao sinalizados a parte em vez de deixar a margem
  parecer boa por engano. O certo e corrigir o cadastro no Tiny, nao o
  codigo. (Mas ver o bug do parametro `id`/`codigo` abaixo - confirmar que
  um "custo_ausente" e real antes de assumir que e isso, pode ser bug.)
- Nao commitar `.env` nem `ml_tokens_*.json` (tokens do Mercado Livre) nem
  dados de pedidos reais de cliente.

## Avisos de campos ajustados

Calibrado em 2026-09-17 contra pedidos reais das 4 contas Tiny. O que mudou
em relacao ao que o codigo assumia originalmente:

- **Canal do pedido**: nao e um campo solto (`nome_ecommerce`/`ecommerce`)
  no nivel raiz - e `pedido["ecommerce"]["nomeEcommerce"]`. Para Mercado
  Livre esse valor ja vem com a subconta embutida (`"ML_AMOSHOESEIRELI 2"`,
  `"ML_AMOOUTLET 1"`), entao **nao precisou** da logica de desempate
  (`identificador_extra`) que a v1 do codigo tinha previsto - removida.
- **Receita do pedido**: usa `pedido["total_pedido"]` direto, em vez de
  recalcular a partir de item x quantidade + frete. Motivo: o TikTok Shop
  aplica desconto de cupom que reduz o `total_pedido` (confirmado com um
  pedido real, desconto de R$100) e recalcular na mao ignorava isso.
- **Tarifa/comissao, frete real do vendedor e estorno de cupom/bonus NAO
  aparecem no JSON do pedido em nenhum canal** - conferido cruzando o pedido
  900314112 (Meli Conta 2) contra o extrato real do Mercado Livre:
  `valor_frete` do Tiny veio "0.00" mas o extrato mostrou R$19,85 de frete
  real; `valor_desconto` veio 0 mas o extrato mostrou R$10,18 de estorno de
  bonus; nenhum campo bate com a tarifa de 14% cobrada. Por isso
  `imposto_pct`/`comissao_pct`/`frete_pct`/`ads_pct` em `config.yaml`
  continuam sendo estimativas manuais, nunca valores lidos do pedido -
  reconciliar exato exigiria integrar com a API de liquidacao de cada
  marketplace, fora do escopo deste projeto.
- **Comissao real do Mercado Livre confirmada em 14%** (extrato do pedido
  900314112), nao 12% como o `config.example.yaml` original estimava -
  corrigido para as 4 contas Meli (as 3 alem da Conta 2 estao marcadas como
  "confirmar" no config, ja que so temos o extrato de uma delas).
- **Identificador do pedido**: `pedido.obter.php` espera o campo `id` do
  pedido (ex.: `900314112`), nao o campo `numero` (ex.: `72060`, o numero de
  exibicao no Tiny) - `numero` e bem menor e e um campo diferente. O codigo
  agora usa `id` de ponta a ponta (busca, dedupe, storage).
- **Item do pedido**: campos batem com o que o codigo ja assumia
  (`codigo`, `quantidade`, `valor_unitario` dentro de `item`), sem ajuste.

### Bugs reais encontrados rodando contra volume real (nao apareceriam com poucos pedidos de teste)

- **`produto.obter.php` espera o parametro `id` (id_produto interno do
  Tiny), NAO `codigo` (SKU)** - apesar do nome do endpoint sugerir o
  contrario. Passar `codigo` retorna erro da API ("O parametro id deve ser
  informado") pra TODO produto. Esse bug fazia ~70% dos pedidos de uma
  rodada real aparecerem como `custo_ausente=true` quando na verdade tinham
  custo cadastrado normalmente (ex.: produto id=898908943 tinha
  `preco_custo=73.49`, id=898221803 tinha `119.99`). `TinyClient.obter_produto`
  ainda usa `id` corretamente (util pra `--debug-produto`), mas depois disso
  o projeto mudou de arquitetura: custo passou a vir de uma **planilha de
  apoio** (`custo_planilha.py`) em vez de consulta ao vivo no Tiny - ver
  secao "Custo dos produtos". `montar_itens` voltou a buscar por `codigo`
  (SKU), que e a chave natural da planilha.
- **Cache de custo "envenenado" por erro temporario de API** (bug historico,
  ja nao existe mais): quando o custo ainda vinha de consulta ao vivo no
  Tiny, `obter_custo_produto` engolia `TinyApiError` (rate limit, timeout) e
  devolvia `None`, que era cacheado como se o produto realmente nao tivesse
  custo - todo pedido seguinte que usasse o MESMO produto na mesma rodada
  ficava com `custo_ausente` incorreto pelo resto da execucao. Ficou moot
  depois que o custo passou a vir da planilha de apoio (sem cache, sem
  chamada de API por pedido) - mantido aqui so como registro historico.
- **Checkpoint avancava mesmo com falha de API no meio do ciclo**: se um
  pedido falhasse ao buscar detalhe (rate limit, timeout), o checkpoint
  ainda avancava pro "agora" no fim do ciclo - o filtro `dataAtualizacao`
  nunca mais traria esse pedido de volta (ele ja estava "atualizado" antes
  do novo checkpoint). 114 pedidos reais de uma conta foram perdidos assim
  antes da correcao (tive que resetar o checkpoint manualmente pra
  recupera-los). Corrigido: o checkpoint so avanca se NENHUM pedido falhou
  por erro de API naquele ciclo.
- **`tiny_identificador` de Meli Conta 3 e Conta 4 estavam errados**: o
  palpite original seguia o padrao `"ML_<NOME_DA_LOJA> <numero>"` (que
  funciona pra Conta 1 e Conta 2), mas Conta 3 e Conta 4 na verdade aparecem
  como `"Mercado Livre 3"` e `"Mercado Livre 4"` (nome generico + numero, ML
  nao e consistente entre as proprias subcontas). Corrigido depois de
  buscar exemplos reais de pedido de cada uma via `--debug-busca` +
  `--debug-pedido` em lote.

### Alertas: nao dependem de rede/credencial pra nao travar o monitor

`enviar_whatsapp`/`enviar_email` agora tratam `KeyError` de credencial
faltando no `.env` como um erro recuperavel (loga e segue), em vez de deixar
a excecao subir e derrubar o `monitor.py --once` inteiro no meio de uma
rodada - importante porque `ativo: true` no `config.yaml` sem credencial
configurada (caso comum durante a calibracao) acontecia bem no primeiro
pedido com margem negativa.
