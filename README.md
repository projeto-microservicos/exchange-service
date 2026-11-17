# Exchange service

API independente em **Python 3.12 + FastAPI**, conforme o [exercício Exchange](https://insper.github.io/platform/2026.2/exercises/exchange/). Consulta cotações na AwesomeAPI e atende o contrato usado por gateway e order. Não depende de Java, de outros repositórios ou de banco de dados.

## Contrato HTTP

```http
GET /exchanges/USD/BRL
id-account: 00000000-0000-0000-0000-000000000001
```

`GET /exchange/USD/BRL` (singular) responde igual: o enunciado declara `/exchanges/{from}/{to}`, mas o exemplo dele e a página BACEN usam `/exchange/...`. O gateway roteia os dois prefixos.

```json
{
  "sell": 5.2,
  "buy": 5.1,
  "date": "2026-11-16 12:00:00",
  "id-account": "00000000-0000-0000-0000-000000000001"
}
```

Os valores acima são ilustrativos; a execução usa o provedor real. `sell=ask` e `buy=bid`. Quando só existe o par inverso, `sell=1/bid` e `buy=1/ask`. Moedas iguais e suportadas retornam 1, com `date` no formato do enunciado (`2026-11-17 14:23:42`, UTC). Nomes de moedas aceitam letras maiúsculas/minúsculas e passam por uma allowlist explícita.

| Situação | HTTP |
|---|---|
| Cotação válida | 200 |
| Header ausente/inválido, moeda ou par não suportado | 422 |
| Timeout, limite do provedor, resposta inválida ou falha de conexão | 503 |

O gateway valida o token e sobrescreve `id-account` com o UUID autenticado. A API interna exige esse UUID, mas não emite tokens. O order chama `http://exchange:8080/exchanges/USD/{currency}` com o mesmo header. O caminho plural `/exchanges` segue o endpoint declarado no exercício e o contrato da equipe.

Health-check: `GET /exchange/health-check` responde 200 sem corpo e sem exigir `id-account`; as probes de startup e liveness chamam esse endpoint. Ele é chamado direto no serviço, não pelo gateway. OpenAPI/Swagger: `/docs`; schema: `/openapi.json`; readiness: `/ready`; `/health` (legado); Prometheus: `/metrics`. Esses endpoints operacionais ficam na rede interna; a rota pública do gateway deve expor somente a API acordada.

## Executar e testar

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv/Scripts/Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m unittest discover -s tests_ci -v
uvicorn app.main:app --host 127.0.0.1 --port 8083
```

Os testes padrão simulam o provedor e cobrem sucesso, par inverso, identidade, dados inválidos, timeout, cache e entrega de eventos. O cenário de integração real é executado separadamente:

```bash
bash scripts/test-infrastructure.sh
bash scripts/scan.sh
```

O teste de infraestrutura cria Redis/RabbitMQ locais com projeto e senha temporários e remove seus contêineres/volumes ao terminar. Não acessa AWS ou o broker da equipe. O scan usa pip-audit e bloqueia qualquer vulnerabilidade conhecida ou análise incompleta nas dependências de runtime.

## Docker Compose

```bash
python scripts/init-env.py
docker network create store-dev  # Uma vez; pule se já existir.
docker compose up --build -d --wait --wait-timeout 180
```

A API fica em `http://localhost:8083`, e outros serviços usam `exchange:8080` na rede `store-dev`. Por padrão, sobe apenas exchange. Para habilitar os gargalos localmente, altere `.env` para `CACHE_ENABLED=true` e `MESSAGING_ENABLED=true` e execute:

```bash
docker compose --profile cache --profile messaging --profile monitoring up --build -d --wait --wait-timeout 180
```

Grafana: `http://localhost:3001`, usuário `admin`, senha `GRAFANA_PASSWORD` do `.env`; Prometheus: `http://localhost:9092`. O gerador cria senhas distintas e permite `--update`, preservando configurações existentes. Mudanças de senha não alteram automaticamente volumes já inicializados.

## Configuração

| Variável | Padrão / uso |
|---|---|
| `EXCHANGE_PROVIDER_URL` | `https://economia.awesomeapi.com.br` |
| `EXCHANGE_PROVIDER_TIMEOUT` | `3` segundos no total, incluindo tentativa inversa |
| `CACHE_ENABLED` | `false`; habilita Redis |
| `REDIS_URL` | `redis://localhost:6379/1` quando habilitado |
| `EXCHANGE_CACHE_TTL` | `60` segundos |
| `MESSAGING_ENABLED` | `false`; habilita eventos de cotação |
| `RABBITMQ_URL` | URL AMQP; use credenciais próprias quando habilitado |
| `EXCHANGE_EVENT_QUEUE_SIZE` | `256` eventos aguardando envio por processo |

O timeout padrão e as conexões limitadas protegem a capacidade do serviço. O cache mantém somente a cotação; a identidade é acrescentada em cada resposta. Cache indisponível ou corrompido usa o provedor, sem inventar taxas. Veja [gargalos e limitações](docs/BOTTLENECKS.md).

`requirements.in` contém as dependências diretas. `requirements.txt` fixa também as transitivas usadas em produção. Ao atualizar o lock, resolva em um ambiente Python 3.12 limpo e execute testes e scan novamente; não congele as ferramentas de desenvolvimento no lock de runtime.

## AWS e CI/CD

[GitHub Actions](.github/workflows/ci-cd.yml) executa testes, audit, build, integração Redis/RabbitMQ e validação de Compose. Publica no ECR com OIDC e faz deploy por digest somente após configurar o ambiente e habilitar o release.

A equipe escolheu o GitHub Actions para todos os serviços. O caminho alternativo por Jenkins foi removido em 17/11 porque não havia Jenkins rodando, e manter dois pipelines publicando no mesmo workload criaria risco de releases concorrentes. Veja [o guia AWS/EKS](docs/DEPLOYMENT.md).

O manifesto atual usa **uma réplica**, `CACHE_ENABLED=false` e `MESSAGING_ENABLED=false`. Não cria Redis, RabbitMQ, RDS, ElastiCache, Amazon MQ ou um cluster. Atualizações sem Pod extra podem causar indisponibilidade breve. HPA e réplicas adicionais ficam para quando houver capacidade aprovada.

## Repositório e autoria

```bash
git clone https://github.com/projeto-microservicos/exchange-service.git
cd exchange-service
```

O agregador `projeto-microservicos/produto` pode fixar este repositório como submodule. Os builds continuam independentes. O serviço segue os exercícios [Exchange](https://insper.github.io/platform/2026.2/exercises/exchange/) e [Bottlenecks](https://insper.github.io/platform/2026.2/exercises/bottlenecks/) e as convenções do projeto da equipe. Houve assistência de IA na implementação, nos testes e na infraestrutura; registre as contribuições reais e observe as regras de divulgação da disciplina.

Autoria: o serviço foi implementado por `adventurescumm` até 16/11. Ele saiu do projeto em 17/11, e o João (`joaorcmr`) assumiu o repositório. Na mesma data, o agente do João (Claude) acrescentou o `GET /exchange/health-check`, a rota no singular, o formato de `date` para moedas iguais e as probes no health-check, corrigiu o grupo do RBAC (`github-deployer`) e removeu o caminho Jenkins. O João revisou e commitou essas mudanças.
