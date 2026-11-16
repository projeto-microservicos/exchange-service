# Exchange service

API independente em **Python 3.12 + FastAPI**, conforme o [exercício Exchange](https://insper.github.io/platform/pt/2026.2/exercises/exchange/). Consulta diretamente o **BACEN/PTAX (Olinda OData)** e atende o contrato usado por gateway e order. Não depende de Java, de outros repositórios ou de banco de dados. A troca da AwesomeAPI segue a orientação do professor sobre problemas de acesso a provedores por IPs da AWS.

## Contrato HTTP

```http
GET /exchanges/USD/BRL
id-account: 00000000-0000-0000-0000-000000000001
```

```json
{
  "sell": 5.2,
  "buy": 5.1,
  "date": "2026-11-16 12:00:00",
  "id-account": "00000000-0000-0000-0000-000000000001"
}
```

Os valores acima são ilustrativos; a execução usa o BACEN real. Para moeda estrangeira → BRL, `sell=cotacaoVenda` e `buy=cotacaoCompra`. Para BRL → moeda estrangeira, `sell=1/cotacaoCompra` e `buy=1/cotacaoVenda`. Para pares cruzados, `sell=vendaOrigem/compraDestino` e `buy=compraOrigem/vendaDestino`, usando as cotações em BRL da mesma data de fechamento.

A fonte é a última **PTAX de fechamento disponível**, pesquisada nos últimos dez dias corridos. Antes do fechamento, em fins de semana e feriados, pode ser usado o último dia útil. O campo `date` preserva o horário publicado pelo BACEN; em pares cruzados, usa o horário mais antigo dos dois componentes daquela data. Não é uma cotação de mercado em tempo real. Se não houver fechamento recente ou data comum entre moedas, retorna 503.

Moedas disponíveis: **AUD, BRL, CAD, CHF, DKK, EUR, GBP, JPY, NOK, SEK e USD**. As dez estrangeiras são as do recurso `Moedas` da PTAX; BRL é a referência matemática. Moedas fora dessa cobertura, inclusive CNY, INR e NZD, retornam 422. Maiúsculas/minúsculas são aceitas. Moedas iguais e suportadas retornam 1 sem consulta externa.

| Situação | HTTP |
|---|---|
| Cotação válida | 200 |
| Header ausente/inválido, moeda ou par não suportado | 422 |
| Timeout, limite do provedor, resposta inválida ou falha de conexão | 503 |

O gateway valida o token e sobrescreve `id-account` com o UUID autenticado. A API interna exige esse UUID, mas não emite tokens. O order chama `http://exchange:8080/exchanges/USD/{currency}` com o mesmo header. O caminho plural `/exchanges` segue o endpoint declarado no exercício e o contrato da equipe.

OpenAPI/Swagger: `/docs`; schema: `/openapi.json`; liveness: `/health`; readiness: `/ready`; Prometheus: `/metrics`. Esses endpoints operacionais ficam na rede interna; a rota pública do gateway deve expor somente a API acordada.

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

Os testes padrão simulam o BACEN e cobrem sucesso, inversão, cruzamento por BRL na mesma data, último fechamento, fins de semana, dados inválidos, timeout total, cache e entrega de eventos. O cenário de integração real de Redis/RabbitMQ é executado separadamente:

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
| `EXCHANGE_PROVIDER_URL` | `https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/` |
| `EXCHANGE_PROVIDER_TIMEOUT` | `3` segundos no total, incluindo as duas consultas paralelas de um par cruzado |
| `CACHE_ENABLED` | `false`; habilita Redis |
| `REDIS_URL` | `redis://localhost:6379/1` quando habilitado |
| `EXCHANGE_CACHE_TTL` | `60` segundos |
| `MESSAGING_ENABLED` | `false`; habilita eventos de cotação |
| `RABBITMQ_URL` | URL AMQP; use credenciais próprias quando habilitado |
| `EXCHANGE_EVENT_QUEUE_SIZE` | `256` eventos aguardando envio por processo |

O timeout padrão e as conexões limitadas protegem a capacidade do serviço. O cache mantém somente a cotação; a identidade é acrescentada em cada resposta. Cache indisponível ou corrompido usa o provedor, sem inventar taxas. Veja [gargalos e limitações](docs/BOTTLENECKS.md).

O adaptador chama `CotacaoMoedaPeriodo` com filtro `tipoBoletim eq 'Fechamento'`, ordenação decrescente e limite de dez registros. Espaços são codificados como `%20`, pois o Olinda interpreta `+` literalmente nessas expressões. A namespace Redis é `exchange:ptax:v1`, para evitar reutilizar cotações antigas da AwesomeAPI. Se um `.env` ou ConfigMap antigo fixa `EXCHANGE_PROVIDER_URL`, atualize-o para o endereço PTAX: o adaptador agora espera o formato BACEN.

Fontes: [documentação oficial PTAX](https://www.bcb.gov.br/conteudo/dadosabertos/BCBDepin/gnastportal-dados-abertostaxas-de-cambio---todos-os-boletins-diarios.pdf), [moedas publicadas pelo BACEN](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/Moedas?%24format=json).

`requirements.in` contém as dependências diretas. `requirements.txt` fixa também as transitivas usadas em produção. Ao atualizar o lock, resolva em um ambiente Python 3.12 limpo e execute testes e scan novamente; não congele as ferramentas de desenvolvimento no lock de runtime.

## AWS e CI/CD

[GitHub Actions](.github/workflows/ci-cd.yml) executa testes, audit, build, integração Redis/RabbitMQ e validação de Compose. Publica no ECR com OIDC e faz deploy por digest somente após configurar o ambiente e habilitar o release. O [Jenkinsfile](Jenkinsfile) é uma alternativa completa para o exercício, usando o mesmo script de release.

Veja [o guia AWS/EKS](docs/DEPLOYMENT.md) e [o guia Jenkins](docs/JENKINS-EKS.md). Escolha um mecanismo para releases automáticos, evitando dois pipelines publicando simultaneamente no mesmo workload.

O manifesto atual usa **uma réplica**, `CACHE_ENABLED=false` e `MESSAGING_ENABLED=false`. Não cria Redis, RabbitMQ, RDS, ElastiCache, Amazon MQ ou um cluster. Atualizações sem Pod extra podem causar indisponibilidade breve. HPA e réplicas adicionais ficam para quando houver capacidade aprovada.

## Repositório e autoria

```bash
git clone https://github.com/projeto-microservicos/exchange-service.git
cd exchange-service
```

O agregador `projeto-microservicos/produto` pode fixar este repositório como submodule. Os builds continuam independentes. O serviço segue os exercícios [Exchange](https://insper.github.io/platform/2026.2/exercises/exchange/) e [Bottlenecks](https://insper.github.io/platform/2026.2/exercises/bottlenecks/) e as convenções do projeto da equipe. Houve assistência de IA na implementação, nos testes e na infraestrutura; registre as contribuições reais e observe as regras de divulgação da disciplina.
