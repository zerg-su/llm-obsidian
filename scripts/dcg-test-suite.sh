#!/usr/bin/env bash
# Smoke-test набор для destructive_command_guard (dcg).
# По 3 теста на каждый включённый pack из config/dcg/config.toml плюс
# baseline из 3 ALLOWED-команд. Считает совпадения с ожиданием по точному
# verdict'у (ALLOWED / BLOCKED / WARN / LOG) и выводит summary с breakdown.
# По умолчанию запускается с временным HOME, чтобы пользовательский allowlist не
# маскировал регрессии. Для проверки live-конфига: DCG_TEST_USE_USER_CONFIG=1.
#
# Запуск:
#   bash scripts/dcg-test-suite.sh
#   bash scripts/dcg-test-suite.sh --print-dcg-bin
#
# Все имена ресурсов заведомо несуществующие (fake-*-99999, nonexistent-*,
# /dev/nonexistent-*, PFAKE99999). dcg test — dry-run, команды не выполняются.
#
# Категории verdict'ов:
#   ALLOWED — паттерн не сматчился (или severity-rule пропустил)
#   BLOCKED — сработал deny (severity critical/high по умолчанию)
#   WARN    — сматчилось, но severity=medium → warning, не блок
#   LOG     — сматчилось, severity=low → лог-запись, не блок

set -uo pipefail

resolve_dcg_bin() {
    local requested="${DCG_BIN:-}"
    local candidate

    if [ -n "$requested" ]; then
        if [ ! -x "$requested" ]; then
            echo "DCG_BIN не указывает на исполняемый файл: $requested" >&2
            return 127
        fi
        candidate="$(cd "$(dirname "$requested")" && pwd -P)/$(basename "$requested")"
        printf '%s\n' "$candidate"
        return 0
    fi

    if command -v dcg >/dev/null 2>&1; then
        command -v dcg
        return 0
    fi

    if [ -n "${HOME:-}" ] && [ -x "$HOME/.local/bin/dcg" ]; then
        printf '%s\n' "$HOME/.local/bin/dcg"
        return 0
    fi

    for candidate in /opt/homebrew/bin/dcg /usr/local/bin/dcg; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    echo "dcg не найден в PATH или стандартных каталогах установки" >&2
    return 127
}

resolve_rc=0
resolved_dcg_bin="$(resolve_dcg_bin)" || resolve_rc=$?
if [ "$resolve_rc" -ne 0 ]; then
    exit "$resolve_rc"
fi
DCG_BIN="$resolved_dcg_bin"

if [ "${1:-}" = "--print-dcg-bin" ]; then
    if [ "$#" -ne 1 ]; then
        echo "usage: bash scripts/dcg-test-suite.sh [--print-dcg-bin]" >&2
        exit 2
    fi
    printf '%s\n' "$DCG_BIN"
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "usage: bash scripts/dcg-test-suite.sh [--print-dcg-bin]" >&2
    exit 2
fi

# By default, validate the repo-shipped dcg policy in isolation. User-level
# allowlists are intentionally ignored so personal exceptions do not mask
# regressions in the portable config. Set DCG_TEST_USE_USER_CONFIG=1 to test the
# machine's live ~/.config/dcg state instead.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "${DCG_TEST_USE_USER_CONFIG:-0}" != "1" ] && [ -f "$REPO_ROOT/config/dcg/config.toml" ]; then
    DCG_TEST_HOME="${DCG_TEST_HOME:-$(mktemp -d /tmp/dcg-test-home.XXXXXX)}"
    mkdir -p "$DCG_TEST_HOME/.config/dcg" "$DCG_TEST_HOME/.local/share/dcg"
    cp "$REPO_ROOT/config/dcg/config.toml" "$DCG_TEST_HOME/.config/dcg/config.toml"
    export HOME="$DCG_TEST_HOME"
    export XDG_CONFIG_HOME="$DCG_TEST_HOME/.config"
    export XDG_DATA_HOME="$DCG_TEST_HOME/.local/share"
fi
# Dispatch executors inherit DCG_CONFIG=config/dcg/task.toml. Base cases must
# still discover the isolated base config above; live mode must discover the
# user's config. Task cases set their explicit config at the call site.
unset DCG_CONFIG

PASS=0
FAIL=0
ALLOWED_COUNT=0
BLOCKED_COUNT=0
WARN_COUNT=0
LOG_COUNT=0
UNKNOWN_COUNT=0
declare -a FAILURES=()
CURRENT_PACK=""

group() {
    CURRENT_PACK="$1"
    printf "\n\033[1;36m── %s ──\033[0m\n" "$1"
}

# expected ∈ {ALLOWED, BLOCKED, WARN, LOG}
run_case() {
    local expected="$1"
    local cmd="$2"
    local actual out

    out=$("$DCG_BIN" test "$cmd" 2>&1)

    if   grep -qE 'Result: BLOCKED' <<<"$out"; then
        actual="BLOCKED"; BLOCKED_COUNT=$((BLOCKED_COUNT+1))
    elif grep -qE 'Result: WARN'    <<<"$out"; then
        actual="WARN";    WARN_COUNT=$((WARN_COUNT+1))
    elif grep -qE 'Result: LOG'     <<<"$out"; then
        actual="LOG";     LOG_COUNT=$((LOG_COUNT+1))
    elif grep -qE 'Result: ALLOWED' <<<"$out"; then
        actual="ALLOWED"; ALLOWED_COUNT=$((ALLOWED_COUNT+1))
    else
        actual="UNKNOWN"; UNKNOWN_COUNT=$((UNKNOWN_COUNT+1))
    fi

    if [[ "$expected" == "$actual" ]]; then
        printf "  \033[32m✔\033[0m  %-7s  %s\n" "$actual" "$cmd"
        PASS=$((PASS+1))
    else
        printf "  \033[31m✗\033[0m  %-7s (expected %s)  %s\n" "$actual" "$expected" "$cmd"
        FAIL=$((FAIL+1))
        FAILURES+=("[$CURRENT_PACK] expected=$expected actual=$actual cmd=$cmd")
    fi
}

run_task_case() {
    local expected="$1"
    local cmd="$2"
    local actual out

    out=$(DCG_CONFIG="$REPO_ROOT/config/dcg/task.toml" "$DCG_BIN" test --agent codex "$cmd" 2>&1)
    if   grep -qE 'Result: BLOCKED' <<<"$out"; then actual="BLOCKED"; BLOCKED_COUNT=$((BLOCKED_COUNT+1))
    elif grep -qE 'Result: WARN'    <<<"$out"; then actual="WARN"; WARN_COUNT=$((WARN_COUNT+1))
    elif grep -qE 'Result: LOG'     <<<"$out"; then actual="LOG"; LOG_COUNT=$((LOG_COUNT+1))
    elif grep -qE 'Result: ALLOWED' <<<"$out"; then actual="ALLOWED"; ALLOWED_COUNT=$((ALLOWED_COUNT+1))
    else actual="UNKNOWN"; UNKNOWN_COUNT=$((UNKNOWN_COUNT+1)); fi

    if [[ "$expected" == "$actual" ]]; then
        printf "  \033[32m✔\033[0m  %-7s  %s\n" "$actual" "$cmd"
        PASS=$((PASS+1))
    else
        printf "  \033[31m✗\033[0m  %-7s (expected %s)  %s\n" "$actual" "$expected" "$cmd"
        FAIL=$((FAIL+1))
        FAILURES+=("[$CURRENT_PACK] expected=$expected actual=$actual cmd=$cmd")
    fi
}

#─────────────────────────────────────────────────────────────
# BASELINE: безобидные команды, ожидаем ALLOWED
#─────────────────────────────────────────────────────────────

group "baseline (ALLOWED)"
run_case ALLOWED "ls -la /tmp/nonexistent-xyz-99999"
run_case ALLOWED "echo hello world"
run_case ALLOWED "git status"

#─────────────────────────────────────────────────────────────
# core
#─────────────────────────────────────────────────────────────

group "core.filesystem"
run_case BLOCKED "rm -rf /var/fake-path-99999"
run_case BLOCKED "rm -rf /home/fake-user-99999/data"
run_case BLOCKED "rm -rf ~/fake-projects-99999"

group "core.git"
run_case BLOCKED "git reset --hard HEAD~1"
run_case BLOCKED "git push --force origin fake-branch-99999"
run_case BLOCKED "git checkout -- fake-99999.txt"

group "base profile local Git"
run_case BLOCKED "git filter-branch --force --tree-filter 'rm -rf .git/fake-99999' HEAD"
run_case BLOCKED "git rebase --onto fake-99999 fake-99998"
run_case ALLOWED "git commit --amend -m 'fake-99999'"
run_case ALLOWED "git cherry-pick fake-99999"
run_case ALLOWED "git add -A"
run_case BLOCKED "git filter-repo --force"
run_case BLOCKED "git reflog expire --expire=now --all"
run_case BLOCKED "git gc --prune=now"
run_case BLOCKED "git submodule deinit --all"

group "task profile practical Git"
run_task_case ALLOWED "git rebase --onto fake-99999 fake-99998"
run_task_case ALLOWED "git commit --amend -m 'fake-99999'"
run_task_case ALLOWED "git cherry-pick fake-99999"
run_task_case ALLOWED "git add -A"
run_task_case BLOCKED "git restore fake-99999.txt"
run_task_case BLOCKED "git reset --hard HEAD~1"
run_task_case BLOCKED "git push origin task/dcg-fixture"
run_task_case BLOCKED "git worktree remove /tmp/fake-worktree-99999"
run_task_case BLOCKED "git filter-repo --force"
run_task_case BLOCKED "git reflog expire --expire=now --all"
run_task_case BLOCKED "git gc --prune=now"
run_task_case BLOCKED "git submodule deinit --all"

#─────────────────────────────────────────────────────────────
# infrastructure
#─────────────────────────────────────────────────────────────

group "infrastructure.terraform"
run_case BLOCKED "terraform destroy -auto-approve"
run_case BLOCKED "terraform destroy -target=fake_module_99999.fake_resource"
run_case BLOCKED "terraform state rm fake_resource.fake_99999"

group "infrastructure.ansible"
run_case BLOCKED "ansible all -m shell -a 'rm -rf /var/fake-99999' -i fake-hosts-99999"
run_case BLOCKED "ansible all -m shell -a 'sudo reboot' -i fake-hosts-99999"
run_case BLOCKED "ansible-playbook -i fake-inv-99999.ini fake-99999-site.yml"

#─────────────────────────────────────────────────────────────
# cloud / api / storage
#─────────────────────────────────────────────────────────────

group "cloud.aws"
run_case BLOCKED "aws ec2 terminate-instances --instance-ids i-fake99999abc"
run_case BLOCKED "aws iam delete-user --user-name fake-user-99999"
run_case BLOCKED "aws kms schedule-key-deletion --key-id fake-key-99999 --pending-window-in-days 7"

group "apigateway.aws"
run_case BLOCKED "aws apigateway delete-rest-api --rest-api-id fake99999"
run_case BLOCKED "aws apigateway delete-stage --rest-api-id fake99999 --stage-name fake-99999"
run_case BLOCKED "aws apigatewayv2 delete-api --api-id fake99999"

group "storage.s3"
run_case BLOCKED "aws s3 rb s3://nonexistent-test-bucket-99999 --force"
run_case BLOCKED "aws s3 rm s3://nonexistent-test-bucket-99999/ --recursive"
run_case BLOCKED "aws s3api delete-bucket --bucket nonexistent-test-bucket-99999"

#─────────────────────────────────────────────────────────────
# kubernetes
#─────────────────────────────────────────────────────────────

group "kubernetes.kubectl"
run_case BLOCKED "kubectl delete namespace fake-test-ns-99999"
run_case BLOCKED "kubectl delete pv fake-pv-99999"
run_case BLOCKED "kubectl delete pvc fake-pvc-99999"

group "kubernetes.helm"
run_case BLOCKED "helm uninstall fake-release-99999 -n fake-ns-99999"
run_case BLOCKED "helm delete fake-release-99999 --purge"
run_case BLOCKED "helm rollback fake-release-99999 0"

group "kubernetes.kustomize"
run_case BLOCKED "kustomize build ./fake-99999/ | kubectl delete -f -"
run_case BLOCKED "kubectl kustomize ./fake-99999/ | kubectl delete -f -"
run_case BLOCKED "kustomize build /tmp/fake-overlays-99999/ | kubectl delete -f -"

#─────────────────────────────────────────────────────────────
# databases
#─────────────────────────────────────────────────────────────

group "database.postgresql"
run_case BLOCKED "psql -c 'DROP DATABASE fake_db_99999'"
run_case BLOCKED "psql -c 'DROP TABLE fake_users_99999 CASCADE'"
run_case BLOCKED "dropdb fake_db_99999"

group "database.mongodb"
run_case BLOCKED "mongo --eval 'db.dropDatabase()' fake-99999"
run_case BLOCKED "mongo fake-99999 --eval 'db.fake_collection_99999.drop()'"
run_case BLOCKED "mongorestore --drop /tmp/fake-99999-dump/"

group "database.redis"
run_case BLOCKED "redis-cli -h 127.0.0.1 -p 1 FLUSHALL"
run_case BLOCKED "redis-cli -h 127.0.0.1 -p 1 FLUSHDB"
run_case BLOCKED "redis-cli -h 127.0.0.1 -p 1 -n 0 DEBUG FLUSHALL"

#─────────────────────────────────────────────────────────────
# containers
#─────────────────────────────────────────────────────────────

group "containers.docker"
run_case BLOCKED "docker system prune -a --volumes -f"
run_case BLOCKED "docker rm -f fake-container-99999"
run_case BLOCKED "docker volume rm fake-volume-99999"

group "containers.podman"
run_case BLOCKED "podman system prune -a -f --volumes"
run_case BLOCKED "podman rm -f fake-container-99999"
run_case BLOCKED "podman volume rm fake-volume-99999"

#─────────────────────────────────────────────────────────────
# system
#─────────────────────────────────────────────────────────────

group "system.disk"
run_case BLOCKED "mkfs.ext4 /dev/nonexistent-disk-99999"
run_case BLOCKED "dd if=/dev/zero of=/dev/nonexistent-disk-99999 bs=1M count=1"
run_case BLOCKED "wipefs -a /dev/nonexistent-disk-99999"

group "system.permissions"
run_case BLOCKED "chmod -R 777 /etc/fake-99999"
run_case BLOCKED "chown -R nobody:nogroup /etc/fake-99999"
run_case BLOCKED "chmod -R 777 /usr/local/fake-99999"

group "system.services"
run_case BLOCKED "systemctl stop fake-service-99999.service"
run_case BLOCKED "systemctl disable fake-service-99999.service"
run_case BLOCKED "systemctl mask fake-service-99999.service"

#─────────────────────────────────────────────────────────────
# remote
#─────────────────────────────────────────────────────────────

group "remote.rsync"
run_case BLOCKED "rsync -a --delete src/ nobody@nonexistent-host-99999.invalid:/tmp/dst/"
run_case BLOCKED "rsync -a --delete-after /var/fake-99999/ /tmp/fake-dst-99999/"
run_case BLOCKED "rsync -a --del src/ /tmp/fake-99999/"

group "remote.scp"
run_case BLOCKED "scp -r ~/fake-99999/ nobody@nonexistent-host-99999.invalid:/etc/"
run_case BLOCKED "scp -r src/ nobody@nonexistent-host-99999.invalid:/"
run_case BLOCKED "scp file nobody@nonexistent-host-99999.invalid:/usr/local/fake-99999"

group "remote.ssh"
run_case BLOCKED "ssh nobody@nonexistent-host-99999.invalid 'rm -rf /var/fake-99999'"
run_case BLOCKED "ssh nobody@nonexistent-host-99999.invalid 'sudo rm -rf /var/fake-99999'"
run_case BLOCKED "ssh nobody@nonexistent-host-99999.invalid 'git reset --hard fake-99999'"

#─────────────────────────────────────────────────────────────
# CI/CD
#─────────────────────────────────────────────────────────────

group "cicd.gitlab_ci"
run_case BLOCKED "glab variable delete fake-var-99999"
run_case BLOCKED "gitlab-runner unregister --name fake-runner-99999"
run_case BLOCKED "glab api -X DELETE projects/99999/variables/fake-99999"

group "cicd.github_actions"
run_case BLOCKED "gh secret delete FAKE_SECRET_99999"
run_case BLOCKED "gh secret remove FAKE_SECRET_99999"
run_case BLOCKED "gh api -X DELETE /repos/fake-org-99999/fake-repo-99999/actions/secrets/FAKE_SECRET_99999"

group "cicd.jenkins"
run_case BLOCKED "java -jar jenkins-cli.jar -s http://nonexistent-99999.invalid delete-job fake-job-99999"
run_case BLOCKED "java -jar jenkins-cli.jar -s http://nonexistent-99999.invalid delete-node fake-node-99999"
run_case BLOCKED "java -jar jenkins-cli.jar -s http://nonexistent-99999.invalid delete-credentials fake-99999 fake-cred-99999"

#─────────────────────────────────────────────────────────────
# secrets
#─────────────────────────────────────────────────────────────

group "secrets.aws_secrets"
run_case BLOCKED "aws secretsmanager delete-secret --secret-id fake-secret-99999 --force-delete-without-recovery"
run_case BLOCKED "aws secretsmanager delete-secret --secret-id fake-secret-99999 --recovery-window-in-days 7"
run_case BLOCKED "aws secretsmanager delete-secret --secret-id fake-secret-99999"

group "secrets.vault"
run_case BLOCKED "vault kv delete secret/fake-99999"
run_case BLOCKED "vault delete secret/fake-99999"
run_case BLOCKED "vault token revoke fake-token-99999"

#─────────────────────────────────────────────────────────────
# platform
#─────────────────────────────────────────────────────────────

group "platform.github"
run_case BLOCKED "gh repo delete fake-org-99999/fake-repo --yes"
run_case BLOCKED "gh release delete v0.0.0-fake-99999 --yes"
run_case BLOCKED "gh issue delete 99999"

group "platform.gitlab"
run_case BLOCKED "glab repo delete fake-namespace-99999/fake-repo-99999 --yes"
run_case BLOCKED "glab release delete v0.0.0-fake-99999"
run_case BLOCKED "glab repo archive fake-namespace-99999/fake-repo-99999"

#─────────────────────────────────────────────────────────────
# dns / loadbalancer
#─────────────────────────────────────────────────────────────

group "dns.cloudflare"
run_case BLOCKED "wrangler dns-records delete --zone fake-99999.example.com --record-id fake-record-99999"
run_case BLOCKED "curl -X DELETE https://api.cloudflare.com/client/v4/zones/fake99999/dns_records/fake88888"
run_case BLOCKED "curl -X DELETE https://api.cloudflare.com/client/v4/zones/fake99999"

group "loadbalancer.elb"
run_case BLOCKED "aws elb delete-load-balancer --load-balancer-name fake-elb-99999"
run_case BLOCKED "aws elbv2 delete-load-balancer --load-balancer-arn arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/fake-99999/abc"
run_case BLOCKED "aws elbv2 delete-target-group --target-group-arn arn:aws:elasticloadbalancing:us-east-1:000000000000:targetgroup/fake-99999/abc"

group "loadbalancer.haproxy"
run_case BLOCKED "service haproxy stop"
run_case BLOCKED "systemctl stop haproxy.service"
run_case BLOCKED "echo 'shutdown frontend fake_99999' | socat stdio /tmp/fake-haproxy-99999.sock"

group "loadbalancer.nginx"
run_case BLOCKED "nginx -s stop"
run_case BLOCKED "service nginx stop"
run_case BLOCKED "rm -rf /etc/nginx/conf.d/fake-99999.conf"

#─────────────────────────────────────────────────────────────
# messaging
#─────────────────────────────────────────────────────────────

group "messaging.kafka"
run_case BLOCKED "kafka-topics --delete --topic fake-topic-99999 --bootstrap-server nonexistent-99999.invalid:9092"
run_case BLOCKED "kafka-consumer-groups --delete --group fake-group-99999 --bootstrap-server nonexistent-99999.invalid:9092"
run_case BLOCKED "kafka-configs --alter --entity-type topics --entity-name fake-99999 --delete-config retention.ms"

group "messaging.rabbitmq"
run_case BLOCKED "rabbitmqctl delete_vhost /fake-99999"
run_case BLOCKED "rabbitmqadmin delete queue name=fake-queue-99999"
run_case BLOCKED "rabbitmqctl reset"

group "messaging.sqs_sns"
run_case BLOCKED "aws sqs delete-queue --queue-url https://sqs.us-east-1.amazonaws.com/000000000000/fake-queue-99999"
run_case BLOCKED "aws sns delete-topic --topic-arn arn:aws:sns:us-east-1:000000000000:fake-topic-99999"
run_case BLOCKED "aws sqs purge-queue --queue-url https://sqs.us-east-1.amazonaws.com/000000000000/fake-queue-99999"

#─────────────────────────────────────────────────────────────
# monitoring
#─────────────────────────────────────────────────────────────

group "monitoring.prometheus"
run_case BLOCKED "curl -X POST http://nonexistent-99999.invalid:9090/api/v1/admin/tsdb/delete_series"
run_case BLOCKED "kubectl delete prometheusrule fake-rule-99999 -n fake-ns-99999"
run_case BLOCKED "grafana-cli plugins uninstall fake-plugin-99999"

group "monitoring.pagerduty"
run_case BLOCKED "pd service delete fake-svc-99999"
run_case BLOCKED "curl -X DELETE -H 'Authorization: Token token=fake-99999' https://api.pagerduty.com/services/PFAKE99999"
run_case BLOCKED "curl -X DELETE -H 'Authorization: Token token=fake-99999' https://api.pagerduty.com/schedules/PFAKE99999"

#─────────────────────────────────────────────────────────────
# search
#─────────────────────────────────────────────────────────────

group "search.elasticsearch"
run_case BLOCKED "curl -X DELETE http://nonexistent-99999.invalid:9200/fake-index-99999"
run_case BLOCKED "curl -X DELETE http://nonexistent-99999.invalid:9200/_all"
run_case BLOCKED "curl -X POST http://nonexistent-99999.invalid:9200/fake-99999/_delete_by_query -d '{\"query\":{\"match_all\":{}}}'"

group "search.opensearch"
run_case BLOCKED "curl -X DELETE http://nonexistent-99999.invalid:9200/fake-os-index-99999"
run_case BLOCKED "curl -X DELETE http://nonexistent-99999.invalid:9200/_all"
run_case BLOCKED "aws opensearch delete-domain --domain-name fake-99999"

#─────────────────────────────────────────────────────────────
# backup
#─────────────────────────────────────────────────────────────

group "backup.rclone"
run_case BLOCKED "rclone delete fake-remote-99999:/fake-path-99999"
run_case BLOCKED "rclone purge fake-remote-99999:/fake-path-99999"
run_case BLOCKED "rclone deletefile fake-remote-99999:/fake-99999/file.txt"

group "backup.restic"
run_case BLOCKED "restic forget --keep-last 0 --prune --repo /tmp/fake-restic-99999"
run_case BLOCKED "restic prune --repo /tmp/fake-restic-99999"
run_case BLOCKED "restic forget all --prune --repo /tmp/fake-99999"

#─────────────────────────────────────────────────────────────
# package_managers
#─────────────────────────────────────────────────────────────

group "package_managers"
run_case BLOCKED "npm unpublish fake-package-99999@1.0.0 --force"
run_case BLOCKED "npm publish fake-package-99999"
run_case BLOCKED "pip uninstall fake-package-99999"

#─────────────────────────────────────────────────────────────
# Summary
#─────────────────────────────────────────────────────────────

echo
printf "\033[1m─────────────────────────────────────────────\033[0m\n"
printf "\033[1mSummary\033[0m\n"
printf "\033[1m─────────────────────────────────────────────\033[0m\n"
TOTAL=$((PASS + FAIL))
printf "  Cases:     %d total  (\033[32m%d pass\033[0m, \033[31m%d fail\033[0m)\n" \
    "$TOTAL" "$PASS" "$FAIL"
printf "  Verdicts:  %d ALLOWED, %d BLOCKED, %d WARN, %d LOG, %d UNKNOWN\n" \
    "$ALLOWED_COUNT" "$BLOCKED_COUNT" "$WARN_COUNT" "$LOG_COUNT" "$UNKNOWN_COUNT"

if (( FAIL > 0 )); then
    echo
    echo "Failures (expected != actual):"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit 1
fi

echo
echo "OK: dcg отбил все destructive-кейсы и пропустил baseline."
exit 0
