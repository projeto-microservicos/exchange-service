#!/usr/bin/env bash
# Only a uniquely named, disposable local Compose project is started and removed.
set -euo pipefail
task_project="exchange-check-$(date +%s)-$$"
export EXCHANGE_TEST_RABBIT_PASSWORD
EXCHANGE_TEST_RABBIT_PASSWORD=$("${PYTHON:-python3}" -c 'import secrets; print(secrets.token_hex(24))')
stack=(docker compose -p "$task_project" -f compose.test.yaml)
cleanup() { "${stack[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
"${stack[@]}" up -d --wait --wait-timeout 180
redis_address=$("${stack[@]}" port cache 6379)
rabbit_address=$("${stack[@]}" port rabbit 5672)
export EXCHANGE_TEST_REDIS_URL="redis://$redis_address/1"
export EXCHANGE_TEST_RABBITMQ_URL="amqp://exchange-test:$EXCHANGE_TEST_RABBIT_PASSWORD@$rabbit_address/"
"${PYTHON:-python3}" -m pytest -q -m integration --junitxml=artifacts/infrastructure.xml
