# Tutorial em PT-BR — Subindo a stack do lado do cliente

Este passo a passo cobre o que o time de DevOps/SRE precisa fazer pra rodar
a demo do **Redis Cloud Autoscaler UI** dentro da rede deles, apontando pra
um banco Redis Cloud Pro próprio.

Tempo estimado: **~10 minutos**, sendo a maior parte preenchendo um `.env`.

---

## O que você vai precisar

1. Uma **VM Linux** (Ubuntu 22.04+, Debian 12+, RHEL 9+, etc) com:
   - **2 vCPU / 4 GB RAM** já é suficiente
   - **Docker Engine** e **docker compose v2**
   - **Conectividade privada** ao endpoint interno do seu Redis Cloud
     (via Private Service Connect no GCP, VPC peering na AWS, ou Transit Gateway)
2. Uma **subscription Redis Cloud Pro** com:
   - Pelo menos 1 banco configurado (qualquer tamanho)
   - **Replication habilitada** (HA — opção padrão do Pro)
   - API keys ativas
3. Acesso ao **endpoint interno na porta `:8070`** (métricas Prometheus
   nativas do Redis Cloud — é como vamos coletar `bdb_*`)
4. **Git**

> 🔒 **Segurança**: a stack faz chamadas autenticadas pra REST API do Redis
> Cloud usando as suas keys, e expõe o banco ao `memtier_benchmark` pra
> gerar carga. Tudo roda dentro da VM — recomendamos colocá-la num
> security group/subnet privada. Pra acesso externo (browser), use VPN,
> bastion SSH com port-forward, ou um proxy reverso com TLS (incluímos um
> exemplo com Caddy mais abaixo).

---

## Passo 1 · Clonar o repo

```bash
git clone https://github.com/gacerioni/redis-cloud-autoscaler-ui.git
cd redis-cloud-autoscaler-ui
```

## Passo 2 · Coletar os dados do seu Redis Cloud

Abra o console em `https://cloud.redis.io` → seu banco → aba **Configuration**.
Anote os **5 valores obrigatórios** abaixo:

| O que você precisa | Onde achar |
|---|---|
| **Endpoint privado** (host:porta) | aba *Configuration* → seção *Endpoints* → "Private endpoint" |
| **Senha do banco** | aba *Security* → "Default user password" |
| **Subscription ID** (numérico) | na URL do console: `/subscriptions/subscription/NNNNNNN/...` |
| **Database ID** (numérico) | na URL do console: `/bdb-view/NNNNNNNN/configuration` |
| **API keys** (User Key + Account Key) | menu superior → ícone de conta → *Access Management* → *API Keys* (crie a "User Key" e copie a "Account Key" da mesma tela) |

> 💡 **O endpoint interno do Prometheus é descoberto automaticamente** pela
> UI no boot (campo `prometheusEndpoint` da subscription). Você pode deixar
> `REDIS_CLOUD_INTERNAL_ENDPOINT=` vazio no `.env` — a stack acha sozinho.

### Validando conectividade antes de subir

Antes de prosseguir, **garanta que sua VM alcança o endpoint privado**.
Da VM, rode:

```bash
nc -zv silent-frog-lemon-72563.db.redis.io 19515   # porta do banco
nc -zv silent-frog-lemon-72563.db.redis.io 8070    # porta de métricas
```

Os dois precisam dar `succeeded`. Se algum falhar, o problema está no
peering/PSC — **resolva isso antes de continuar**, senão a stack não vai
funcionar.

> 💡 **Dica**: O endpoint interno (`internal.cXXXXX...`) e o endpoint
> privado (`redis-NNNNN.internal.cXXXXX...`) **resolvem pro mesmo IP**
> dentro da rede privada — você pode usar qualquer um pro teste de `nc`.

## Passo 3 · Configurar o `.env`

Crie o arquivo de configuração a partir do template:

```bash
cp .env.example .env
$EDITOR .env
```

Preencha **as 5 variáveis obrigatórias** com os dados que você anotou:

```bash
# Endpoint privado do banco (host:porta)
REDIS_HOST_AND_PORT=silent-frog-lemon-72563.db.redis.io:19515

# Senha do banco
REDIS_PASSWORD=uxrwnX1mneuiDUdJUZQPCsOVN36bt5Jh

# API keys (cuidado: vão pra dois headers diferentes)
REDIS_CLOUD_API_KEY=<sua User Key>             # vai como x-api-secret-key
REDIS_CLOUD_ACCOUNT_KEY=<sua Account Key>      # vai como x-api-key

# IDs numéricos
REDIS_CLOUD_SUBSCRIPTION_ID=3284776
DEMO_DB_ID=14345819

# Endpoint Prometheus interno — deixa VAZIO pra auto-discovery,
# OU sobrescreva manualmente se quiser forçar um valor específico:
# REDIS_CLOUD_INTERNAL_ENDPOINT=internal.cXXXXX.region-mz.gcp.cloud.rlrcp.com
REDIS_CLOUD_INTERNAL_ENDPOINT=
```

Ajuste também os **baselines** pra refletir o tamanho atual do seu banco:

```bash
BASELINE_OPS=2500        # throughput configurado no console
BASELINE_MEM_GB=2.5      # ⚠️ dataset size (não memory limit!) — veja nota abaixo
BURST_OPS=5000           # alvo do scale UP (ex: 2x baseline)
THROUGHPUT_CEILING=10000 # teto duro de proteção
MEMORY_STEP_GB=2         # quanto sobe a memória por trigger
MEMORY_CEILING_GB=5      # teto duro de memória
```

> ⚠️ **Atenção sobre `BASELINE_MEM_GB`**: use o valor de **dataset size** que
> aparece na UI do Redis Cloud (a quantidade de memória útil que você pode
> usar pra dados). Com HA habilitado, a REST API do Redis Cloud retorna
> `memoryLimitInGb` igual ao **dobro** disso, porque a memória física é
> alocada pra master + réplica. A UI mostra ambos os valores como
> `Dataset: 2.5 GB · with HA: 5 GB physical` automaticamente.

E o **branding** (aparece no header da UI):

```bash
DEMO_CLIENT_NAME=Globo
DEMO_TAGLINE=Plataforma Jarvis — pico de tráfego de jogo
```

## Passo 3a · Habilitar Basic Auth (opcional, recomendado)

Pra evitar que qualquer um que descubra a URL aperte `Reset to baseline`
sem querer:

```bash
# no .env
UI_AUTH_USERNAME=admin
UI_AUTH_PASSWORD=secret42      # qualquer string não-vazia
```

A UI vai pedir login na primeira request (browser dialog do Basic Auth).
Deixa `UI_AUTH_PASSWORD=` (vazio) pra desabilitar.

## Passo 3b · Memory scaling (OFF por default)

Por decisão de produto, **scaling de memória vem DESLIGADO por default** —
escalar RAM tem impacto direto no custo do shard. A UI continua mostrando
memória usada como info contextual, mas:

- nenhum alerta `IncreaseMemory` é criado no Prometheus
- nenhuma scaling rule de memória é registrada no autoscaler
- o foco da demo fica em **throughput**

Se quiser habilitar (com cuidado):
```bash
MEMORY_SCALING_ENABLED=true
```

## Passo 4 · Subir a stack

```bash
docker compose up -d
```

Isso vai puxar 5 imagens e subir 4 containers persistentes mais 1 container
init que roda 1 vez e morre:

| Container | Função |
|---|---|
| `autoscaler-init` (Alpine) | Renderiza `prometheus.yml` e `alert.rules` no volume compartilhado. Roda 1× e termina. |
| `prometheus` | Coleta métricas `bdb_*` do seu Redis Cloud a cada 5s |
| `alertmanager` | Recebe alertas do Prometheus e dispara webhook pro autoscaler |
| `autoscaler` | App Java do Field Engineering — quando recebe webhook, chama a REST API do Redis Cloud pra escalar |
| `autoscaler-ui` | A UI web que você vai acessar (porta `8000` por padrão) |

Acompanhe o boot:

```bash
docker compose logs -f ui
```

Você deve ver, em ~15 segundos:
```
bootstrap: registering scaling rules
registered IncreaseThroughput
registered IncreaseMemory
UI ready
```

Se aparecer **"autoscaler unreachable"**, espere mais 30 segundos —
o autoscaler Java demora ~15s pra subir. Se passar de 1 minuto:

```bash
docker compose logs autoscaler   # ver se o Spring Boot subiu OK
```

## Passo 5 · Acessar a UI

Por design, **só a porta `8000` (UI) é exposta ao host**. Prometheus,
Alertmanager e Autoscaler ficam apenas na rede interna do Compose
— mais seguro e menos firewall pra abrir.

```bash
# Se você está na própria VM:
curl -sS http://localhost:8000/api/health   # deve retornar {"status":"ok"}

# Se você está num laptop e quer abrir o browser, faça um port-forward SSH:
ssh -L 8000:localhost:8000 ubuntu@<sua-vm>

# Aí abra no navegador:
open http://localhost:8000
```

Se você setou `UI_AUTH_PASSWORD`, o browser vai pedir usuário e senha.

> Pra debug, você pode abrir as outras 3 portas temporariamente:
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.expose.yml up -d
> ```
> Isso publica `:8080` (autoscaler), `:9090` (prometheus), `:9093` (alertmanager).

A primeira coisa que aparece deve ser:
- **Status do banco** ● *active* + throughput e memória configurados
- **Live metrics** mostrando ops/sec real do banco
- **Alerts** ambos *inactive* (porque ainda não há carga acima do threshold)
- **Scheduled scale-down** com status `✓ at baseline`

Se o **Status** estiver carregando infinitamente, ou aparecer um erro
`DB API: 401` no rodapé, suas API keys estão **trocadas** — abra `.env` e
inverta os valores de `REDIS_CLOUD_API_KEY` e `REDIS_CLOUD_ACCOUNT_KEY`,
depois `docker compose restart ui`.

## Passo 6 · Disparar carga (Match surge / Kickoff peak)

Na UI, role até o card **Load generator**. Escolha um preset:

- **Baseline traffic** — não dispara nada (apenas tráfego baixo)
- **Sustained burst** — dispara scale UP de throughput em ~40s
- **Dual scale** — dispara throughput E memory (com `key-pattern=R:R` e values de 1KB)
- **Memory fill** — só memory (writes puros)

Clique **Start load**. Aguarde:
- ~10s: `ramping up…` no painel "Live metrics"
- ~10–30s: ops/sec aparece (~30k+)
- ~30–60s: alerta vira **firing** (vermelho)
- ~+10s: toast `🚀 Scaled UP` aparece, throughput configurado pula

Pra parar e voltar ao baseline:
- **Stop load** → o card "Scheduled scale-down" arma um countdown (5min por padrão)
- Quer voltar agora? **Reset now** no mesmo card → REST API direto

---

## Customizando os thresholds

Tudo configurável via `.env`:

| Variável | Significado | Default |
|---|---|---|
| `THROUGHPUT_THRESHOLD_PCT` | dispara quando ops/sec > X% do baseline | `80` |
| `THROUGHPUT_THRESHOLD_FOR` | precisa sustentar acima por X | `30s` |
| `MEMORY_THRESHOLD_PCT` | dispara quando memória > X% do limite | `80` |
| `MEMORY_THRESHOLD_FOR` | precisa sustentar acima por X | `30s` |
| `BURST_OPS` | alvo do scale UP de throughput | `40000` |
| `THROUGHPUT_CEILING` | teto duro de throughput (default = cobre pico BBB de 33k com folga) | `40000` |
| `MEMORY_STEP_GB` | quanto a memória sobe por trigger (só se memory scaling ON) | `2` |
| `MEMORY_CEILING_GB` | teto duro de memória (só se memory scaling ON) | `5` |
| `MEMORY_SCALING_ENABLED` | habilita scale UP de memória (OFF por default) | `false` |
| `UI_AUTH_PASSWORD` | Basic Auth na UI (vazio = sem auth) | `` (vazio) |
| `AUTO_RESET_SECONDS` | tempo até o reset agendado pro baseline | `300` |

Mudou algo? Aplique sem rebuild:

```bash
docker compose restart ui prometheus
```

Em 5 segundos a nova política está ativa.

---

## Deploy público com HTTPS (opcional, via Caddy)

Quer expor a UI com domínio próprio e TLS automático? Use a overlay:

1. Aponte um **A record DNS** pra IP público da VM
2. Libere portas **80 e 443** no security group
3. No `.env`:
   ```bash
   DEMO_DOMAIN=autoscaler.minharede.globo.com
   DEMO_EMAIL=devops@globo.com
   ```
4. Suba com a overlay:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
   ```

O Caddy puxa o certificado Let's Encrypt automaticamente em ~30s. Sem
certbot, sem cron de renovação — ele cuida sozinho.

---

## Solução de problemas

### UI fica em "connecting..." pra sempre

```bash
docker compose logs ui --tail 50
```

Provavelmente erro no boot. Causas comuns:
- `REDIS_CLOUD_*_KEY` trocados → 401 → autoscaler não consegue registrar rules
- `REDIS_CLOUD_INTERNAL_ENDPOINT` errado → Prometheus não scrape

### Alerts ficam `unknown` ao invés de `inactive`

O Prometheus ainda não fez o primeiro scrape do endpoint `:8070`. Aguarde
10-15s. Se persistir:

```bash
docker compose logs prometheus --tail 20
```

Procure por `connection refused` ou `no such host`. Se for esse o caso, sua
VM **não tem reachability ao endpoint privado** — volte ao Passo 2.

### Quero limpar tudo e começar de novo

```bash
docker compose down -v   # mata containers + volumes (config do prom)
rm .env
cp .env.example .env
$EDITOR .env
docker compose up -d
```

### O banco encheu de chaves do teste, quero limpar sem mexer no autoscaler

Na UI → card **Admin** → **Flush database** (preserva as Rule/Task documents
do autoscaler que vivem no mesmo banco — não precisa se preocupar).

### Quero forçar o banco pro tamanho baseline AGORA (sem esperar 5 min)

Na UI → card **Scheduled scale-down** → **Reset now**.
Ou via admin: **Reset to baseline**.

---

## Limpeza completa (descomissionar)

```bash
docker compose down -v
docker rmi $(docker images "gacerioni/redis-cloud-autoscaler-ui*" -q) 2>/dev/null
# E não esqueça de deletar o banco / subscription pelo console se foi só pra teste
```

---

## Referências

- Projeto base do autoscaler (Java, suportado pelo Field Engineering da Redis):
  https://github.com/redis-field-engineering/redis-cloud-autoscaler
- Este repo (UI + bootstrap):
  https://github.com/gacerioni/redis-cloud-autoscaler-ui
- Documentação Redis Cloud Pro:
  https://redis.io/docs/latest/operate/rc/

Dúvidas? Abre uma issue ou chama o time da Redis.
