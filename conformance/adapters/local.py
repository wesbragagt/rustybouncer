"""Disposable local-process validation against PgBouncer and rustybouncer.

Requires initdb, pg_ctl, postgres, pgbouncer on PATH and a built Rust binary.
No Docker, system service, or existing database is used or modified.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run(argv: list[str], **kwargs):
    return subprocess.run(argv, check=True, timeout=60, **kwargs)


class Environment:
    def __init__(self, directory: Path, report: Path, docker: bool = False, mode: str = "transaction", docker_poolers: bool = False):
        self.directory, self.report = directory, report
        self.pg_port, self.reference_port, self.target_port = free_port(), free_port(), free_port()
        self.processes = []
        self.logs = []
        self.pg_started = False
        self.docker = docker
        self.mode = mode
        self.docker_poolers = docker_poolers
        self.service_containers: list[str] = []
        self.container = "pooler-conformance-" + uuid.uuid4().hex[:10]

    def start_process(self, name: str, command: list[str]):
        log = (self.report / f"{name}.log").open("w")
        self.logs.append(log)
        if self.docker_poolers:
            executable = str(Path(shutil.which(command[0]) or command[0]).resolve())
            container = self.container + "-" + name
            self.service_containers.append(container)
            command = ["docker", "run", "--rm", "--name", container, "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
                       "-v", "/nix/store:/nix/store:ro", "-v", f"{self.directory}:{self.directory}",
                       "-v", f"{executable}:{executable}:ro", "--entrypoint", executable, "postgres:16", *command[1:]]
        process = subprocess.Popen(command, stdout=log, stderr=log)
        self.processes.append(process)
        return process

    def start_postgres(self):
        if self.docker:
            run(["docker", "run", "-d", "--name", self.container, "-p", f"127.0.0.1:{self.pg_port}:5432",
                 "-e", "POSTGRES_USER=conformance", "-e", "POSTGRES_DB=postgres", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16"], stdout=subprocess.DEVNULL)
            self.pg_started = True
            deadline = time.monotonic() + 30
            while True:
                result = subprocess.run(["docker", "exec", self.container, "pg_isready", "-h", "127.0.0.1", "-U", "conformance"],
                                        capture_output=True, timeout=5, check=False)
                if result.returncode == 0:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Docker PostgreSQL did not become ready")
                time.sleep(.1)
        pgdata = self.directory / "postgres"
        run(["initdb", "-D", str(pgdata), "-U", "conformance", "--auth=trust", "--no-locale", "--encoding=UTF8"], stdout=subprocess.DEVNULL)
        run(["pg_ctl", "-D", str(pgdata), "-l", str(self.report / "postgres.log"), "-o",
             f"-h 127.0.0.1 -p {self.pg_port} -k {self.directory}", "-w", "start"], stdout=subprocess.DEVNULL)
        self.pg_started = True

    def start(self):
        self.start_postgres()
        userlist = self.directory / "users.txt"
        userlist.write_text('"conformance" ""\n')
        config = self.directory / "pgbouncer.ini"
        config.write_text(f'''[databases]
postgres = host=127.0.0.1 port={self.pg_port} dbname=postgres
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {self.reference_port}
unix_socket_dir = {self.directory}
auth_type = trust
auth_file = {userlist}
admin_users = conformance
pool_mode = {self.mode}
default_pool_size = 2
max_client_conn = 24
max_prepared_statements = 4
query_wait_timeout = 1
server_login_retry = 1
''')
        self.start_process("pgbouncer", ["pgbouncer", str(config)])
        target = self.directory / "rustybouncer.toml"
        target.write_text(f'''listen = "127.0.0.1:{self.target_port}"
max_clients = 24
route_selects = true
probe_interval_ms = 50
probe_timeout_ms = 500
wait_timeout_ms = 8000
startup_timeout_ms = 2000
probe_user = "conformance"
probe_database = "postgres"
[[nodes]]
name = "postgres"
host = "127.0.0.1"
port = {self.pg_port}
''')
        binary = Path(os.environ.get("RUSTYBOUNCER_BIN", ROOT / "target/debug/rustybouncer"))
        self.start_process("rustybouncer", [str(binary), str(target)])
        for port in (self.reference_port, self.target_port):
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.5):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"local adapter endpoint on port {port} did not start") from None
                    time.sleep(.05)

    def close(self):
        for container in reversed(self.service_containers):
            subprocess.run(["docker", "rm", "-fv", container], timeout=15, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in self.logs:
            log.close()
        if self.pg_started and self.docker:
            result = subprocess.run(["docker", "logs", self.container], capture_output=True, text=True, timeout=10, check=False)
            (self.report / "postgres.log").write_text(result.stdout + result.stderr)
            subprocess.run(["docker", "rm", "-fv", self.container], timeout=15, check=False, stdout=subprocess.DEVNULL)
        elif self.pg_started:
            subprocess.run(["pg_ctl", "-D", str(self.directory / "postgres"), "-m", "immediate", "-w", "stop"],
                           timeout=15, check=False, stdout=subprocess.DEVNULL)

    def execute(self, repeat: int, match: str) -> int:
        outcomes = {}
        for name, port in ((f"pgbouncer-{self.mode}", self.reference_port), ("rustybouncer-routing", self.target_port)):
            env = dict(os.environ, POOLER_DSN=f"host=127.0.0.1 port={port} user=conformance dbname=postgres sslmode=disable",
                       POSTGRES_DSN=f"host=127.0.0.1 port={self.pg_port} user=conformance dbname=postgres sslmode=disable")
            if name.startswith("pgbouncer"):
                env["POOLER_ADMIN_DSN"] = env["POOLER_DSN"].replace("dbname=postgres", "dbname=pgbouncer")
            manifest = json.loads((ROOT / f"conformance/manifests/{name}.json").read_text())
            manifest["features"]["postgres_restart"] = True
            restart = ["docker", "restart", self.container] if self.docker else ["pg_ctl", "-D", str(self.directory / "postgres"), "-m", "fast", "-l", str(self.report / "postgres.log"), "-w", "restart"]
            manifest["actions"] = {"postgres_restart": restart}
            manifest_path = self.report / f"{name}-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2))
            env["CONFORMANCE_ALLOW_FAULTS"] = "1"
            command = [sys.executable, "-m", "conformance", "--manifest", str(manifest_path),
                       "--json", str(self.report / f"{name}.json"), "--junit", str(self.report / f"{name}.xml"),
                       "--repeat", str(repeat), "--match", match]
            result = subprocess.run(command, cwd=ROOT, env=env, timeout=300, check=False)
            outcomes[name] = result.returncode
        metadata = {"recorded_at": datetime.now(timezone.utc).isoformat(), "python": sys.version, "platform": platform.platform(),
                    "pgbouncer_version": subprocess.check_output(["pgbouncer", "--version"], text=True).strip(),
                    "host_postgres_version": subprocess.check_output(["postgres", "--version"], text=True).strip(),
                    "target_sha256": hashlib.sha256(Path(os.environ.get("RUSTYBOUNCER_BIN", ROOT / "target/debug/rustybouncer")).read_bytes()).hexdigest()}
        (self.report / "comparison.json").write_text(json.dumps({"metadata": metadata, "exit_codes": outcomes, "environment": "docker-postgres-and-poolers" if self.docker_poolers else ("docker-postgres-local-poolers" if self.docker else "local-processes"),
             "release_acceptance": "partial: one environment, declared capabilities only"}, indent=2))
        return int(any(outcomes.values()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--match", default="")
    parser.add_argument("--docker-postgres", action="store_true")
    parser.add_argument("--docker-poolers", action="store_true", help="Linux/Nix optional adapter: run the same built pooler binaries in Docker")
    parser.add_argument("--reference-mode", choices=("session", "transaction", "statement"), default="transaction")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    for command in ("initdb", "pg_ctl", "postgres", "pgbouncer"):
        if not shutil.which(command):
            parser.error(f"missing optional local-adapter program: {command}")
    with tempfile.TemporaryDirectory(prefix="pooler-conformance-") as directory:
        environment = Environment(Path(directory), args.output, args.docker_postgres, args.reference_mode, args.docker_poolers)
        try:
            environment.start()
            return environment.execute(args.repeat, args.match)
        finally:
            environment.close()


if __name__ == "__main__":
    raise SystemExit(main())
