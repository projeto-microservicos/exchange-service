# Gargalos: implementação e validação

Referência: [Bottlenecks — Insper 2026.2](https://insper.github.io/platform/2026.2/exercises/bottlenecks/). As medidas são adaptadas ao domínio de consulta de cotações; este serviço não cobra pagamentos nem mantém pedidos ou contas.

| Área | Implementação | Ativação atual |
|---|---|---|
| Cache | Redis com TTL, valores sem identidade e exclusão de consultas concorrentes por par | Opcional localmente; desligado no EKS |
| Observabilidade | Prometheus, métricas de requisições/provedor/cache/eventos e dashboard Grafana provisionado | Perfil `monitoring` local; integrar ao monitoramento existente no EKS |
| Mensageria | Eventos `QuoteFetched` em worker assíncrono com fila limitada e confirmação do RabbitMQ | Opcional localmente; desligada no EKS |
| Balanceamento | Service ClusterIP, probes e manifesto HPA opcional | Uma réplica no cluster atual |
| Vulnerabilidades | Lock transitivo de runtime e pip-audit antes de publicar | Obrigatório no GitHub Actions |

## Cache e limite do provedor

`CACHE_ENABLED=true` habilita Redis com `EXCHANGE_CACHE_TTL=60`. A chave `exchange:v1:{source}:{target}` contém apenas `sell`, `buy` e `date`. A identidade da conta nunca é persistida no cache. Pares inválidos, erros e taxas inválidas não são cacheados.

Cada processo serializa cache misses para o mesmo par e reconsulta o cache dentro do lock. Isso reduz chamadas duplicadas dentro de uma réplica; não é um lock distribuído entre réplicas. A allowlist de moedas limita o número de locks possíveis.

As escritas Redis usam transação para associar cotação e TTL. Timeouts de cache são de 200 ms; falhas de conexão/leitura/escrita preservam a consulta ao provedor. Não há fallback para cotações inventadas ou cache expirado. Moedas iguais retornam a identidade matemática sem consultar dependências.

O cliente HTTP reutiliza conexões, limita o pool a 20 conexões e tem um orçamento total padrão de três segundos para as tentativas direta/inversa. Não faz tentativas ilimitadas em respostas 429. O provedor pode manter seu próprio cache e impor limites: [documentação da AwesomeAPI](https://docs.awesomeapi.com.br/api-de-moedas).

## Eventos de cotação

Com `MESSAGING_ENABLED=true`, uma cotação obtida do provedor oferece um evento ao worker, sem aguardar RabbitMQ no caminho HTTP. Cache hits e moedas iguais não geram novos eventos de consulta.

Contrato: exchange direto durável `store.exchanges`, routing key `quoted`, fila durável `exchanges.quoted`, mensagens persistentes JSON e `message_id=eventId`. Campos: `eventId`, `type=QuoteFetched`, `source`, `target`, `sell`, `buy`, `date` e `occurredAt`. Os valores monetários no evento são strings decimais; não há identidade da conta.

O worker usa publisher confirms, publicação obrigatoriamente roteada e até três tentativas com o mesmo `eventId`. A fila interna tem 256 posições por processo. Fila cheia ou tentativas esgotadas descartam a telemetria e incrementam métricas. O broker mantém no máximo 10.000 mensagens com TTL de um dia e rejeita novas publicações quando a fila está cheia.

**É telemetria best effort, não uma outbox transacional.** Não há banco/transação de negócio neste serviço. Uma queda do processo pode perder eventos pendentes, e uma confirmação ambígua seguida de retry pode duplicar eventos. Não use esse fluxo para cobrança ou efeitos financeiros obrigatórios; consumidores devem deduplicar por `eventId`. Eventos novos exigem nomes/filas próprios se seu contrato mudar.

## Observabilidade

O dashboard provisionado mostra vazão, p95, proporção 5xx, cache hit ratio, chamadas ao provedor, resultados de publicação, fila interna e memória do processo. Labels não incluem UUIDs, URLs completas ou caminhos arbitrários. Health checks e scrapes não entram na latência HTTP da API.

`/exchange/health-check` (startup e liveness), `/ready` (readiness) e `/health` verificam a disponibilidade do processo; não chamam o provedor. Falhas externas aparecem nas respostas e métricas, sem provocar reinícios de todos os Pods. Redis e RabbitMQ opcionais não bloqueiam readiness.

Para evidenciar ganho, compare a mesma carga com cache ligado/desligado e registre chamadas externas, latência p95 e erros. Os testes funcionais comprovam comportamento, não um ganho de desempenho medido.

## Kubernetes e custos

O manifesto padrão usa uma réplica com `maxSurge=0` e `maxUnavailable=1`, aceitando indisponibilidade breve para evitar um Pod extra durante rollout. Não aplique `k8s/hpa.yaml` enquanto a capacidade for insuficiente; ele começa em duas réplicas. Quando o HPA administrar o workload, remova `spec.replicas` da fonte declarativa e revise a estratégia de rollout.

Cache/mensageria permanecem explicitamente desligados no Deployment, mesmo com ConfigMaps antigos. Para habilitá-los futuramente, remova esses overrides, provisione Redis/RabbitMQ dentro do cluster e configure endpoints/Secrets. O pipeline não aplica HPA ou dependências e não cria recursos AWS pagos.

## Verificação

Os testes cobrem bid/ask, inversão, moedas iguais, identidade, timeout total, rate limit, dados inválidos, fallback de cache, retries e descarte de eventos. `scripts/test-infrastructure.sh` verifica cache/TTL com duas contas e confirmação de uma mensagem em Redis/RabbitMQ reais e descartáveis.

O scan deve produzir um inventário completo em `artifacts/vulnerabilities.json`; advisories e falhas de ferramenta bloqueiam o release. Esse resultado cobre bibliotecas Python, não substitui o scanning de imagem/sistema operacional nem uma avaliação de configuração da conta AWS.
