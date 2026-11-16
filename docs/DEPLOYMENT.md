# AWS: ECR, EKS e GitHub Actions

O workflow `.github/workflows/ci-cd.yml` usa o mesmo formato operacional dos serviços da equipe: verificação em PRs e publicação/deploy em `main`. A configuração de infraestrutura é responsabilidade do dono da conta. O pipeline não cria ECR, EKS, RDS, caches ou brokers.

## Variáveis GitHub

Configure em **Settings → Secrets and variables → Actions → Variables**:

| Variável | Uso |
|---|---|
| `AWS_REGION` | Região do ECR e EKS |
| `AWS_ACCOUNT_ID` | Conta esperada, 12 dígitos |
| `AWS_ROLE_ARN` | Role IAM para OIDC |
| `ECR_REPOSITORY` | ECR existente; padrão `exchange-service` |
| `EKS_CLUSTER_NAME` | Cluster existente; obrigatório no deploy |
| `K8S_NAMESPACE` | Padrão `store` |
| `KUBECTL_VERSION` | Versão explícita compatível, `vX.Y.Z`; obrigatória no deploy |
| `IMAGE_PLATFORM` | `linux/amd64` ou `linux/arm64`; padrão amd64 |
| `RELEASE_ENABLED` | `true` publica a cada push em `main`; ausente mantém somente CI |
| `DEPLOY_ENABLED` | `true` também faz deploy automático; padrão desligado |

Execuções manuais em `main` publicam e usam sua própria opção `deploy`, inicialmente desmarcada. Outras branches e PRs não executam release. O workflow serializa builds de `main`; um rollout em andamento não é cancelado.

Não cadastre chaves AWS permanentes ou kubeconfig no GitHub. As tags contêm commit, origem do pipeline, run ID e tentativa; o manifesto aplica o digest retornado pelo ECR. ARM64 habilita emulação e seleciona nós da arquitetura correspondente.

## Identidade e acesso

O administrador prepara o provedor IAM OIDC de GitHub com audience `sts.amazonaws.com`, e uma role baseada em:

- [Trust policy](github-actions-trust-policy.example.json): substitua `ACCOUNT_ID`; restringe a `projeto-microservicos/exchange-service`, branch `main`.
- [IAM policy](github-actions-iam-policy.example.json): substitua `REGION`, `ACCOUNT_ID` e `CLUSTER_NAME`, e confira o nome do ECR.

Confira o claim `sub` emitido pelo repositório. Personalização, identificação imutável com IDs ou adição de GitHub Environment exigem ajustar a condição exata. Veja [OIDC com AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

Para cluster que já usa access entries, o administrador associa a role ao grupo e aplica o RBAC:

```bash
aws eks create-access-entry --region "$AWS_REGION" --cluster-name "$EKS_CLUSTER_NAME" \
  --principal-arn "$AWS_ROLE_ARN" --type STANDARD --kubernetes-groups github-actions-exchange
kubectl -n "$K8S_NAMESPACE" apply -f k8s/github-actions-rbac.yaml
```

Se a entry existir, preserve outros grupos ao atualizá-la. Não altere automaticamente o modo de autenticação do cluster. As roles dos nós também precisam de permissão de pull do ECR. O runner precisa alcançar o endpoint Kubernetes; endpoint privado exige conectividade à VPC, além de OIDC.

## Configuração inicial

```bash
cp k8s/config.example.yaml k8s/config.local.yaml
# Revise o provedor e as opções, mantendo cache/mensageria false.
kubectl -n "$K8S_NAMESPACE" apply -f k8s/config.local.yaml
```

O ConfigMap `exchange-config` deve existir. `exchange-secrets` é opcional nesta configuração, pois o serviço não requer banco ou chave de API para a consulta pública usada. Se habilitar Redis autenticado ou RabbitMQ, mantenha as URLs com credenciais em Secret, preparado pelo operador, e altere `REQUIRES_SECRET=true` no pipeline. O release nunca sobrescreve ConfigMaps/Secrets ou endpoints.

O manifesto padrão mantém uma réplica e cache/mensageria explicitamente desligados. O Service `exchange:8080` é ClusterIP. Exponha a API somente pelo gateway autenticado, que define `id-account`. Atualizações sem Pod extra podem causar indisponibilidade breve. Não aplique HPA ou dependências enquanto a capacidade não for liberada.

O processo precisa de saída HTTPS para o provedor, DNS e certificados válidos. Não precisa de PostgreSQL/RDS. Configuração de rede, quotas, orçamento e provisionamento ficam fora do script de release.

O provedor é o BACEN/PTAX: `EXCHANGE_PROVIDER_URL=https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/`. Um ConfigMap já existente com o endereço da AwesomeAPI precisa ser atualizado separadamente do release. Em 16/11/2026 foi confirmada resposta válida da PTAX a uma consulta HTTPS executada a partir do pod auth já existente no EKS `eks-store`; essa verificação não implantou exchange nem criou recursos. Repita a validação na rede real do serviço ao fazer o deploy.

## Ativar e diagnosticar

Primeiro execute manualmente em `main` com `deploy` desmarcado. Com ECR, IAM, rede, ConfigMap e acesso EKS prontos, faça um deploy manual. Habilite `RELEASE_ENABLED`/`DEPLOY_ENABLED` somente se desejar automação contínua.

O script verifica conta AWS, ECR e ConfigMap antes de construir a imagem; no deploy, renderiza Kustomize, faz dry-run no servidor, aplica somente Deployment/Service e aguarda rollout. Uma falha de rollout não desfaz automaticamente a atualização: inspecione Pods e reimplante um digest anterior se necessário.

Artefatos: `pytest.xml`, `infrastructure.xml`, `vulnerabilities.json`, `release.json` e `deployment.yaml`. Os dois últimos registram imagem, digest, commit e estado do deploy, sem valores de Secrets. Docker config e kubeconfig temporários são removidos ao encerrar.

Os testes offline dos scripts validam sucesso e falhas sem AWS. Eles não comprovam as permissões ou conectividade da conta real. Configure somente o mecanismo de release escolhido; [Jenkins](JENKINS-EKS.md) é a alternativa solicitada para o exercício.
