#!/usr/bin/env bash
# 阿里云 Ubuntu 一键部署：PostgreSQL + 爬虫栈 + systemd 常驻
# 用法：bash setup_cloud.sh <DB_PASSWORD>  （在 /opt/amazon-crawler 下执行）
set -euo pipefail

DB_PASSWORD="${1:?用法: setup_cloud.sh <DB_PASSWORD>}"
APP_DIR="/opt/amazon-crawler"
cd "$APP_DIR"

echo "=== [1/8] 系统依赖 ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql postgresql-client >/dev/null
systemctl enable --now postgresql

echo "=== [2/8] Python 虚拟环境 ==="
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
# 云上代理模式只走 http_html 路径：selenium/geckodriver 不需要（worker 延迟导入）
# tomli：Python 3.10（Ubuntu 22.04）缺 tomllib 时的兼容包，3.11+ 装了也无害
./.venv/bin/pip install --quiet 'psycopg[binary]>=3.2,<4' tomli

echo "=== [3/8] PostgreSQL 角色 + 数据库 ==="
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='crawler') THEN
    CREATE ROLE crawler LOGIN PASSWORD '${DB_PASSWORD}';
  ELSE
    ALTER ROLE crawler PASSWORD '${DB_PASSWORD}';
  END IF;
END \$\$;
SQL
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='amazon_us'" | grep -q 1; then
  echo "数据库 amazon_us 已存在，跳过恢复（如需重灌：dropdb 后重跑）"
else
  sudo -u postgres createdb -O crawler amazon_us
  echo "=== [4/8] 恢复本地数据（275 商品 + 批次历史 + 937 待爬） ==="
  PGPASSWORD="$DB_PASSWORD" pg_restore -h 127.0.0.1 -U crawler -d amazon_us --no-owner --no-privileges deploy/amazon_us_20260909.dump
fi

echo "=== [5/8] 目录结构 + 熔断门 ==="
mkdir -p data/amazon_us state logs
# 代理多 IP 模式不用门，但写一个干净门防 worker 报缺文件
printf '{"date":"%s","count":0,"consecutive":0,"paused_until":null}\n' "$(date -u +%F)" > state/captcha_gate.json

echo "=== [6/8] systemd 服务 ==="
install -m 644 deploy/amazon-crawler-coordinator.service /etc/systemd/system/
install -m 644 deploy/amazon-crawler-console.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable amazon-crawler-coordinator amazon-crawler-console

echo "=== [7/8] 启动 ==="
systemctl restart amazon-crawler-console
systemctl restart amazon-crawler-coordinator
sleep 3

echo "=== [8/8] 自检 ==="
systemctl --no-pager --lines=3 status amazon-crawler-console amazon-crawler-coordinator | head -30 || true
curl -s "http://127.0.0.1:8771/api/tenants" | head -c 300 && echo
echo ""
echo "部署完成。控制台: http://127.0.0.1:8771（SSH 隧道访问）；数据: amazon_us / 租户 amazon_us_main"
