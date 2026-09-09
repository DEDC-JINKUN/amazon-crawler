"""一键部署驱动：本机 → 阿里云 Ubuntu。

用法（密码只经环境变量传递，不落盘）：
  set DEPLOY_HOST=1.2.3.4
  set DEPLOY_USER=root
  set DEPLOY_PASSWORD=xxxx
  python deploy/deploy.py            # 全流程：传密钥→上传→安装→自检
  python deploy/deploy.py --check    # 只做连通性+状态自检
  python deploy/deploy.py --cmd "systemctl status amazon-crawler-coordinator"

流程：
  1. 用密码登录一次，安装本机公钥到 authorized_keys（之后免密）
  2. SFTP 上传代码包（scripts/config/console/schema/tests/清单/dump/部署件）
  3. 生成服务器 .env（随机新 DB 密码 + ZooProxy 凭证）
  4. 远程执行 setup_cloud.sh（PG + venv + systemd）
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
KEY_PATH = DEPLOY / "id_ed25519"
PUBKEY_PATH = DEPLOY / "id_ed25519.pub"
REMOTE_APP = "/opt/amazon-crawler"

UPLOAD_DIRS = ["scripts", "config", "console", "schema", "tests"]
UPLOAD_FILES = [
    "amazon_us_asin_manifest.csv",
    "requirements.txt",
    "requirements-postgres.txt",
    "requirements-dev.txt",
]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".pytest_tmp", ".trae", ".venv", "tools", "data", "state", "logs"}
EXCLUDE_SUFFIXES = (".exe", ".pyc", ".ps1", ".bat", ".xlsx", ".dump.md")


def read_local_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(ROOT / ".env", encoding="utf-8") as f:
        for line in f:
            m = re.match(
                r"^\s*(ZOOPROXY_HOST|ZOOPROXY_USERNAME|ZOOPROXY_PASSWORD|DATAIMPULSE_HOST|DATAIMPULSE_USERNAME|DATAIMPULSE_PASSWORD)\s*=\s*(.+)$",
                line,
            )
            if m:
                env[m.group(1)] = m.group(2).strip()
    return env


def build_server_env(db_password: str) -> str:
    local = read_local_env()
    lines = [
        "# 服务器端环境（systemd EnvironmentFile；chmod 600）",
        f"AMAZON_US_POSTGRES_DSN=host=127.0.0.1 port=5432 dbname=amazon_us user=crawler password={db_password}",
        f"POSTGRES_PASSWORD={db_password}",
    ]
    for src, dst in (
        ("ZOOPROXY_USERNAME", "ZOO_PROXY_USERNAME"),
        ("ZOOPROXY_PASSWORD", "ZOO_PROXY_PASSWORD"),
        ("DATAIMPULSE_USERNAME", "DATAIMPULSE_PROXY_USERNAME"),
        ("DATAIMPULSE_PASSWORD", "DATAIMPULSE_PROXY_PASSWORD"),
    ):
        if src in local:
            lines.append(f"{dst}={local[src]}")
    return "\n".join(lines) + "\n"


def connect(host: str, user: str, password: str | None = None, use_key: bool = True) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {"hostname": host, "username": user, "timeout": 20}
    if use_key and KEY_PATH.exists():
        kwargs["key_filename"] = str(KEY_PATH)
    if password:
        kwargs["password"] = password
        kwargs["allow_agent"] = False
        kwargs["look_for_keys"] = False
    client.connect(**kwargs)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 600) -> tuple[int, str]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out + (f"\n[stderr]\n{err}" if err.strip() else "")


def ensure_ssh_key() -> None:
    if KEY_PATH.exists() and PUBKEY_PATH.exists():
        return
    import subprocess
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY_PATH), "-C", "amazon-crawler-deploy"],
        check=True, capture_output=True,
    )
    print(f"[key] 生成部署密钥 {KEY_PATH.name}")


def install_pubkey(client: paramiko.SSHClient) -> None:
    pub = PUBKEY_PATH.read_text().strip()
    cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys; "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    code, out = run(client, cmd)
    if code != 0:
        raise SystemExit(f"公钥安装失败: {out}")
    print("[key] 公钥已安装，后续免密登录")


def upload_bundle(sftp: paramiko.SFTPClient) -> int:
    def mkdirs(remote_dir: str) -> None:
        parts = remote_dir.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                sftp.mkdir(cur)

    count = 0
    mkdirs(REMOTE_APP)

    def put_file(local: Path, remote: str) -> None:
        nonlocal count
        mkdirs(str(Path(remote).parent))
        sftp.put(str(local), remote)
        count += 1

    for d in UPLOAD_DIRS:
        for p in (ROOT / d).rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if any(part in EXCLUDE_PARTS for part in p.parts):
                continue
            if p.suffix in EXCLUDE_SUFFIXES:
                continue
            put_file(p, f"{REMOTE_APP}/{rel}")

    for name in UPLOAD_FILES:
        put_file(ROOT / name, f"{REMOTE_APP}/{name}")

    # 部署件：setup 脚本、systemd 单元、Linux 配置、DB dump、代理自检
    for name in ("setup_cloud.sh", "amazon-crawler-coordinator.service",
                 "amazon-crawler-console.service", "amazon_us.linux.human.toml",
                 "amazon_us_20260909.dump", "proxy_probe.py"):
        put_file(DEPLOY / name, f"{REMOTE_APP}/deploy/{name}")
    return count


def main() -> int:
    args = sys.argv[1:]
    host = os.environ.get("DEPLOY_HOST", "")
    user = os.environ.get("DEPLOY_USER", "root")
    password = os.environ.get("DEPLOY_PASSWORD", "")

    if not host:
        print("需要环境变量: DEPLOY_HOST（DEPLOY_USER 默认 root，DEPLOY_PASSWORD 首次用）")
        return 2

    if args and args[0] == "--cmd":
        client = connect(host, user, password or None)
        code, out = run(client, " ".join(args[1:]), timeout=120)
        print(out)
        client.close()
        return code

    if args and args[0] == "--check":
        client = connect(host, user, password or None)
        for cmd in (
            "uname -a",
            "lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2",
            f"curl -s http://127.0.0.1:8771/api/tenants | head -c 200",
            "systemctl is-active amazon-crawler-coordinator amazon-crawler-console 2>&1",
        ):
            code, out = run(client, cmd, timeout=30)
            print(f"$ {cmd}\n{out}")
        client.close()
        return 0

    # ---- 全流程部署 ----
    print(f"[1/5] 连接 {user}@{host}")
    client = connect(host, user, password=password or None, use_key=False)
    ensure_ssh_key()
    install_pubkey(client)
    client.close()

    print("[2/5] 上传代码包")
    client = connect(host, user, use_key=True)
    sftp = client.open_sftp()
    n = upload_bundle(sftp)

    db_password = secrets.token_urlsafe(18)
    env_content = build_server_env(db_password)
    with sftp.file(f"{REMOTE_APP}/.env", "w") as f:
        f.write(env_content)
    sftp.chmod(f"{REMOTE_APP}/.env", 0o600)
    sftp.chmod(f"{REMOTE_APP}/deploy/setup_cloud.sh", 0o755)
    sftp.close()
    print(f"      已上传 {n + 2} 个文件（含 .env 与 dump）")

    print("[3/5] 服务器端安装（PG + venv + systemd，2-5 分钟）")
    code, out = run(client, f"bash {REMOTE_APP}/deploy/setup_cloud.sh '{db_password}'", timeout=900)
    print(out)
    if code != 0:
        print("安装失败，输出如上")
        return 1

    print("[4/5] 代理出口自检（服务器 → ZooProxy → amazon.com）")
    code, out = run(
        client,
        f"cd {REMOTE_APP} && set -a && . ./.env && set +a && ./.venv/bin/python deploy/proxy_probe.py",
        timeout=180,
    )
    print(f"      {out.strip()}")

    print("[5/5] 完成。批次创建：POST http://127.0.0.1:8771/api/batches/json（经 SSH 隧道）")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
