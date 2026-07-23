"""sandbox_boot - 启动时自动拉起 AIO Sandbox 容器（PLAN §6.1 / 一键启动）

设计要点：
1. 端口探活通 → 直接 return（容器已在跑）
2. 探活不通 → `docker inspect`：容器存在但停了就 `docker start`；不存在就 `docker run`
3. 轮询端口最多 60s
4. 没装 docker / docker 失败 → 打印友好错误，不 raise（让 mcp_client 后续给一致报错）
5. opt-out：LAB_AUTOSTART_SANDBOX=false 跳过整个流程（保留手动控制权）
6. 容器参数从 .env 读：AIO_SANDBOX_IMAGE / AIO_SANDBOX_PORT / AIO_SANDBOX_MCP_URL
7. **workspace bind-mount**：宿主 WORKSPACE_DIR → 容器 /workspace（让 sandbox_convert_to_markdown 等
   能直接读 PDF/DOCX）；老容器若没有此挂载会打印一次性迁移提示
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from infra.net_probe import probe_port

CONTAINER_NAME = "aio-sandbox"
SANDBOX_WORKSPACE_MOUNT = "/workspace"  # 容器内的统一工作目录


def _host_workspace_dir() -> Path:
    return Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_state() -> str | None:
    """返回容器 state（running / exited / ...）；不存在返回 None"""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def _image_exists_locally(image: str) -> bool:
    """本地是否已有该镜像；用于区分"首次拉镜像"与"仅创建容器"的日志措辞。"""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _container_has_workspace_mount(host_workspace: Path) -> bool:
    """检查现有容器是否把宿主 WORKSPACE_DIR 挂载到了 SANDBOX_WORKSPACE_MOUNT。

    存在但缺挂载 → 返回 False（调用方据此决定打印迁移提示）
    容器不存在 / docker 报错 → 返回 True（不影响后续逻辑）
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Mounts}}", CONTAINER_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return True  # 容器不存在或 inspect 失败，不打扰
        mounts = json.loads((r.stdout or "[]").strip() or "[]")
        host_resolved = str(host_workspace)
        for m in mounts:
            src = str(m.get("Source", ""))
            dst = str(m.get("Destination", ""))
            # Source 与 host_workspace 需匹配（容忍尾斜杠 / symlink 解析差异）
            if dst == SANDBOX_WORKSPACE_MOUNT and (
                src == host_resolved or Path(src).resolve() == host_workspace
            ):
                return True
        return False
    except Exception:
        return True


def _docker_start(log=print) -> bool:
    try:
        r = subprocess.run(
            ["docker", "start", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f"[sandbox] docker start stderr: {(r.stderr or '').strip()}")
        return r.returncode == 0
    except Exception as e:
        log(f"[sandbox] docker start exception: {type(e).__name__}: {e}")
        return False


def _docker_run(image: str, port: int, host_workspace: Path, log=print) -> bool:
    """首次创建容器；镜像不在本地会自动拉（可能几分钟）

    挂载宿主 workspace 到 /workspace（让 sandbox 工具能读 PDF/DOCX）
    失败时打印 docker stderr，便于排查（常见：WSL2 docker-credential-desktop.exe / 网络拉镜像失败 / 端口占用）
    """
    try:
        r = subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER_NAME,
                "--security-opt", "seccomp=unconfined", "--shm-size", "2g",
                "-p", f"{port}:8080",
                "-v", f"{host_workspace}:{SANDBOX_WORKSPACE_MOUNT}",
                "-e", "DISABLE_JUPYTER=true", "-e", "DISABLE_CODE_SERVER=true",
                image,
            ],
            capture_output=True, text=True, timeout=600,  # 给拉镜像留 10 分钟
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            log(f"[sandbox] docker run failed (exit={r.returncode}):\n  {stderr}")
            # 友好提示：识别 WSL2 凭据助手坑
            if "docker-credential-desktop.exe" in stderr or "exec format error" in stderr:
                log(
                    "[sandbox] 检测到 WSL2 + Docker Desktop 凭据助手错误。\n"
                    "  解决：\n"
                    "    cp ~/.docker/config.json ~/.docker/config.json.bak\n"
                    "    echo '{}' > ~/.docker/config.json\n"
                    "  原因：~/.docker/config.json 指向 Windows .exe 凭据助手，\n"
                    "       从 WSL2 Linux 侧 exec 失败。该镜像是公共仓库，无需登录。"
                )
        return r.returncode == 0
    except Exception as e:
        log(f"[sandbox] docker run exception: {type(e).__name__}: {e}")
        return False


def ensure_sandbox(log=print) -> bool:
    """检测并按需拉起 sandbox 容器；返回端口最终是否通。"""
    if os.getenv("LAB_AUTOSTART_SANDBOX", "true").lower() in {"false", "0", "no"}:
        return True  # 用户禁用了自动启动；交给 mcp_client 探活报错

    url = os.getenv("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp")
    image = os.getenv(
        "AIO_SANDBOX_IMAGE",
        "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest",
    )
    port = int(os.getenv("AIO_SANDBOX_PORT", "8080"))
    host_workspace = _host_workspace_dir()
    host_workspace.mkdir(parents=True, exist_ok=True)

    # 一次性迁移提示：旧容器若没有 workspace 挂载，agent 在沙箱内永远读不到 PDF
    if _docker_available() and not _container_has_workspace_mount(host_workspace):
        log(
            "[sandbox] ⚠️ 检测到旧容器没有 workspace bind-mount。"
            f"sandbox 工具将无法读 {host_workspace} 下的 PDF/DOCX。\n"
            "  请运行：  docker rm -f aio-sandbox\n"
            "  然后重启 cli.py（会自动用新挂载重建容器）。"
        )
        # 继续执行：旧容器仍可运行，只是文件读不到；让用户主动决定是否重建。

    if probe_port(url):
        return True

    if not _docker_available():
        log("[sandbox] 未检测到 docker；请先装 docker 或手动起容器。")
        return False

    state = _container_state()
    if state == "running":
        # 容器在跑但端口未通；等等看（健康检查可能还没过）
        log("[sandbox] 容器 running 但端口未通，等待健康检查...")
    elif state in {"exited", "created", "paused", "dead"}:
        log(f"[sandbox] 容器存在（{state}），尝试 docker start...")
        if not _docker_start(log=log):
            log("[sandbox] docker start 失败；请检查 `docker logs aio-sandbox`。")
            return False
    else:
        if _image_exists_locally(image):
            log("[sandbox] 容器不存在，docker run 创建（本地已有镜像，几秒就绪）...")
        else:
            log(f"[sandbox] 容器不存在，docker run 创建（首次拉镜像约 2.29GB，可能几分钟）...")
        if not _docker_run(image, port, host_workspace, log=log):
            log("[sandbox] docker run 失败；可手动跑：docker run -d --name aio-sandbox ...")
            return False

    # 轮询端口最多 60s
    for _ in range(60):
        if probe_port(url):
            log(f"[sandbox] 就绪（{url}）")
            return True
        time.sleep(1)

    log(f"[sandbox] 等待 60s 仍未就绪（{url}）；可 `docker logs aio-sandbox` 排查。")
    return False


def _docker_rm(log=print) -> bool:
    """docker rm -f aio-sandbox；容器不存在也算成功。"""
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True
        # 容器本来就不在 → 视为成功
        if "No such container" in (r.stderr or ""):
            return True
        log(f"[sandbox] docker rm stderr: {(r.stderr or '').strip()}")
        return False
    except Exception as e:
        log(f"[sandbox] docker rm exception: {type(e).__name__}: {e}")
        return False


def recreate_sandbox(log=print) -> bool:
    """删除现有容器 + 复位 MCP/sandbox_tools 缓存 + 重新拉起。

    用途：`/done --clear` 不仅清宿主 workspace，也清容器内的 pip 全局包 / /tmp /
    长跑进程残留，让下次任务从干净容器开始。
    """
    if os.getenv("LAB_AUTOSTART_SANDBOX", "true").lower() in {"false", "0", "no"}:
        log("[sandbox] LAB_AUTOSTART_SANDBOX=false，跳过重建（请手动 docker rm 后重启 cli）")
        return True
    if not _docker_available():
        log("[sandbox] 未检测到 docker，跳过重建")
        return False

    if _container_state() is not None:
        if _docker_rm(log=log):
            log(f"[sandbox] {CONTAINER_NAME} 已删除")
        else:
            log(f"[sandbox] docker rm {CONTAINER_NAME} 失败；继续尝试重建")

    # 复位上层单例（容器换了，旧 MCP session / tool wrapper 已死）
    try:
        from mcp_client import reset_mcp_client
        reset_mcp_client()
    except Exception as e:
        log(f"[sandbox] reset_mcp_client 失败（继续）：{type(e).__name__}: {e}")
    try:
        import tools.sandbox_tools as _st
        _st._tools_cache = None
    except Exception as e:
        log(f"[sandbox] 清 sandbox_tools._tools_cache 失败（继续）：{type(e).__name__}: {e}")

    # 重新拉起（最多等 60s 端口就绪）
    return ensure_sandbox(log=log)
