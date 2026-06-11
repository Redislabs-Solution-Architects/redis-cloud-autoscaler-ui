# Tutorial PT-BR — subindo a stack na sua infra

Stack Docker autocontida: autoscaler oficial da Redis + Prometheus + Alertmanager + UI web.
Quando o tráfego do seu banco Redis Cloud Pro cruza o threshold, ele escala sozinho via REST API.

Tempo total: **~10 minutos**.

---

## Pré-requisitos

- VM Linux (2 vCPU / 4 GB), x86_64 ou arm64
- **Docker Engine + Compose v2** — confira com `docker compose version` (tem que responder `v2.x` ou maior)
- Subscription **Redis Cloud Pro** com um banco e API keys ativas
- Rota privada da VM até o Redis Cloud (PSC, VPC peering ou Transit Gateway)

Sem o Compose v2? O `docker-compose` do `apt` é o v1 e **não funciona** (os containers saem com `exit code 5`). Instale assim:

```bash
sudo apt-get remove -y docker-compose
curl -fsSL https://get.docker.com | sudo sh
docker compose version
```

## 1 · Clonar

```bash
git clone https://github.com/Redislabs-Solution-Architects/redis-cloud-autoscaler-ui.git
cd redis-cloud-autoscaler-ui
```

**Build interno** (Artifactory, ECR, GAR…)? O build é autocontido — nada é baixado dos autores em runtime:

```bash
docker build -t registry.suaempresa.com/redis-cloud-autoscaler-ui:1.0 .
docker push registry.suaempresa.com/redis-cloud-autoscaler-ui:1.0
# e no .env (passo 3): UI_IMAGE=registry.suaempresa.com/redis-cloud-autoscaler-ui:1.0
```

Sem registry interno, pule — a stack usa a imagem pública do Docker Hub.

## 2 · Validar conectividade

Da VM, antes de qualquer outra coisa:

```bash
nc -zv <endpoint-privado-do-banco> <porta-do-banco>   # ex: redis-12345.internal.cXXXXX... 12345
nc -zv <endpoint-privado-do-banco> 8070               # métricas nativas do Redis Cloud
```

Os dois precisam dar `succeeded`. Falhou? O problema é rede (PSC/peering) — resolva antes de continuar.

## 3 · Configurar o `.env`

```bash
cp .env.example .env
vim .env
```

**Conexão (6 campos):**

| Variável | Onde achar |
|---|---|
| `REDIS_HOST_AND_PORT` | console → banco → *Configuration* → private endpoint (host:porta) |
| `REDIS_PASSWORD` | console → banco → *Security* → default user password |
| `REDIS_CLOUD_ACCOUNT_KEY` | console → *Access Management* → API Keys → **Account key** (pública) |
| `REDIS_CLOUD_API_KEY` | mesma tela → **User key** (secreta) |
| `REDIS_CLOUD_SUBSCRIPTION_ID` | número na URL do console |
| `DEMO_DB_ID` | número na URL do console |

**Dimensionamento (4 campos) — ajuste pro SEU banco:**

| Variável | O que é |
|---|---|
| `BASELINE_OPS` | throughput configurado no seu banco hoje |
| `BASELINE_MEM_GB` | **dataset size** do console (não o memory limit — com HA o limit é 2×) |
| `BURST_OPS` | pra onde o scale-up pula. **Isso é custo** — escolha conscientemente |
| `THROUGHPUT_CEILING` | teto duro; o autoscaler nunca passa disso |

Regras de edição: comentários só em linha própria, valores sem aspas, arquivo com LF.
`REDIS_CLOUD_INTERNAL_ENDPOINT` pode ficar vazio — a stack descobre sozinha via REST API.

## 4 · Subir

```bash
docker compose up -d
docker compose ps        # esperado: 4 containers healthy (o init roda 1x e sai)
```

Confirme o boot:

```bash
docker logs autoscaler-ui 2>&1 | grep registered
# esperado: registered IncreaseThroughput
```

Só **uma** regra por default. Scaling de memória vem desligado (`MEMORY_SCALING_ENABLED=false`) porque tem custo direto — ligue só se quiser.

## 5 · Acessar a UI

Só a porta `8000` é publicada. Do seu laptop:

```bash
ssh -L 8000:localhost:8000 usuario@<vm>
# browser: http://localhost:8000
```

Vai expor pra rede? Sete `UI_AUTH_PASSWORD` no `.env` (Basic Auth) e recicle: `docker compose up -d --force-recreate ui`.

## 6 · Testar o autoscaling

Na UI → card **Load generator** → preset **Sustained burst** → **Start load**.

O que esperar:

| Tempo | Evento |
|---|---|
| ~40s | gráfico de ops/sec sobe |
| ~50s | alerta `IncreaseThroughput` fica `pending` (debounce de 30s) |
| ~80s | alerta `firing` → webhook → autoscaler age |
| ~2min | throughput configurado pula pro `BURST_OPS`, banco volta a `active` |

> Os presets têm intensidade absoluta (calibrados pra um banco de ~25k ops/sec). Em banco pequeno, qualquer preset dispara o scale-up — esperado, não é bug.

**Stop load** → o card *Scheduled scale-down* arma um countdown (`AUTO_RESET_SECONDS`, default 5 min) e o banco volta ao baseline sozinho. Pressa? **Reset now**.

Scale-down é **agendado, não reativo** — por design. Reativo causa yo-yo de shards em produção. A memória nunca é tocada no reset (a menos que você tenha ligado memory scaling).

## Troubleshooting

| Sintoma | Causa | Fix |
|---|---|---|
| `prometheus`/`alertmanager` saem com `exit code 5` | Compose v1 | instale o v2 (pré-requisitos) → `docker compose down -v && docker compose up -d` |
| `autoscaler-init` sai com exit 5 | config inválida | `docker logs autoscaler-init` — a mensagem diz exatamente o que corrigir (keys trocadas = HTTP 500; key com lixo; sub não é Pro) |
| UI fica em `connecting…` | erro no boot | `docker compose logs ui --tail 50` |
| Alertas ficam `unknown` | Prometheus não alcança o `:8070` | volte ao passo 2 |
| Quero recomeçar do zero | — | `docker compose down -v && docker compose up -d` |
| Banco encheu de chave de teste | — | UI → Admin → **Flush database** (preserva os metadados do autoscaler) |

## HTTPS público (opcional)

Domínio próprio + Let's Encrypt automático via Caddy: aponte um DNS pra VM, libere 80/443, preencha `DEMO_DOMAIN`/`DEMO_EMAIL` no `.env` e suba com:

```bash
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

## Referências

- Autoscaler upstream (Redis Field Engineering): https://github.com/redis-field-engineering/redis-cloud-autoscaler
- Este repo: https://github.com/Redislabs-Solution-Architects/redis-cloud-autoscaler-ui
- Redis Cloud Pro: https://redis.io/docs/latest/operate/rc/

Dúvidas? Abra uma issue ou chame o time da Redis.
