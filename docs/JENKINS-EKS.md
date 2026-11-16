# Jenkins: pipeline alternativa para ECR/EKS

O `Jenkinsfile` atende o exercício de CI/CD e usa `scripts/release-eks.sh`, compartilhado com GitHub Actions. Não é necessário instalar Jenkins para usar o workflow da equipe. Evite releases automáticos simultâneos pelos dois mecanismos.

## Agente

Configure um agente Linux confiável com label `eks-deploy`, Python 3.12 com venv, Git, Bash, Docker/Compose, AWS CLI v2 e kubectl compatível com o EKS. O daemon Docker precisa estar acessível ao agente; um Pod Jenkins não o recebe automaticamente. O agente também precisa alcançar PyPI, ECR, o provedor e o endpoint do cluster.

Plugins: Pipeline, Git, JUnit e Credentials Binding. AWS Credentials é necessário somente para a alternativa de credenciais cadastradas descrita abaixo. O pipeline cria `.venv-ci`, executa pytest, testes dos scripts, pip-audit e integração descartável Redis/RabbitMQ antes de publicar.

## Identidade AWS

Prefira a role IAM do agente via instance profile ou Pod Identity/IRSA. A AWS CLI usa a cadeia padrão. Anexe uma política baseada em [jenkins-iam-policy.example.json](jenkins-iam-policy.example.json), substituindo região, conta e cluster. As permissões de pull dos nós EKS são separadas.

Se houver credenciais AWS gerenciadas no Jenkins, informe o ID em `AWS_CREDENTIALS_ID`; o pipeline usa o binding somente no estágio de release. Deixe vazio para usar a role do agente. Não armazene kubeconfig no Git.

O administrador associa a role ao grupo Kubernetes `jenkins-exchange` pelo mecanismo de autenticação existente e aplica:

```bash
kubectl -n "$K8S_NAMESPACE" apply -f k8s/jenkins-rbac.yaml
```

O RBAC gerencia Deployment/Service no namespace e permite as leituras necessárias; não cria infraestrutura. Prepare `exchange-config` como descrito no [guia AWS](DEPLOYMENT.md). O Secret é opcional enquanto as dependências extras estiverem desligadas.

## Job e parâmetros

Crie uma Pipeline **from SCM**, URL `https://github.com/projeto-microservicos/exchange-service.git`, branch `*/main`, Script Path `Jenkinsfile`. Use somente código confiável nesse agente com identidade AWS. Habilite webhook/polling conforme sua instalação existente.

Preencha `AWS_REGION`, `AWS_ACCOUNT_ID`, `ECR_REPOSITORY`, `EKS_CLUSTER_NAME`, `K8S_NAMESPACE` e `IMAGE_PLATFORM` com os recursos existentes. `DEPLOY=false` publica somente no ECR; habilite depois de preparar o cluster. `AWS_CREDENTIALS_ID` é opcional. Credenciais de checkout Git e identidade AWS têm finalidades diferentes.

O pipeline gera tags `commit-jenkins-BUILD_NUMBER`, aplica digest e arquiva os relatórios mesmo em falha. O JUnit registra pytest; os testes de infraestrutura também produzem `artifacts/infrastructure.xml`. Se passar a exigir Secrets, ajuste `REQUIRES_SECRET` no Jenkinsfile.

O pipeline limita execuções concorrentes do mesmo job. Escolha um único job de release por workload; esse bloqueio não coordena jobs diferentes nem GitHub Actions. Cache e mensageria permanecem desligados no manifesto do cluster até a aprovação de capacidade.
