"""sandbox_boot - 启动时自动拉起 AIO Sandbox 容器。

设计要点：
1. 端口探活成功 -> 直接返回（容器已在跑）。
2. 探活失败 -> `docker inspect`：容器存在但已停 -> `docker start`；
   不存在 -> `docker run`。
3. 轮询端口最多 60s。
4. 未装 docker / docker 失败 -> 打印友好错误，不抛异常
   （后续由 mcp_client 给出一致报错）。
5. 可关闭：LAB_AUTOSTART_SANDBOX=false 跳过整个流程（保留手动控制）。
6. 容器参数从 .env 读取：AIO_SANDBOX_IMAGE / AIO_SANDBOX_PORT / AIO_SANDBOX_MCP_URL。
7. **workspace 绑定挂载**：host WORKSPACE_DIR -> 容器 /workspace（使
   sandbox_convert_to_markdown 等能直接读 PDF/DOCX）；无此挂载的旧容器
   会收到一次性迁移提示。
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from config.runtime import get_settings
from infra.net_probe import probe_port

CONTAINER_NAME = "aio-sandbox"
SANDBOX_WORKSPACE_MOUNT = "/workspace"  # 容器侧统一工作区目录


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_state() -> str | None:
    """返回容器状态（running / exited / ...）；不存在返回 None。"""
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
    """检查镜像是否已在本地；用于区分“首次拉镜像”与“仅创建容器”的日志文案。"""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _container_has_workspace_mount(host_workspace: Path) -> bool:
    """检查现有容器是否已将 host WORKSPACE_DIR 挂载到 SANDBOX_WORKSPACE_MOUNT。

    存在但缺挂载 -> 返回 False（由调用方决定是否打迁移提示）。
    容器不存在 / docker 出错 -> 返回 True（不影响后续逻辑）。
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
            # Source 与 host_workspace 须匹配（容忍尾部斜杠 / 软链解析差异）
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
            log(f"[sandbox] docker start 失败，stderr：{(r.stderr or '').strip()}")
        return r.returncode == 0
    except Exception as e:
        log(f"[sandbox] docker start 异常：{type(e).__name__}: {e}")
        return False


def _docker_run(image: str, port: int, host_workspace: Path, log=print) -> bool:
    """首次创建容器；本地无镜像时自动拉取（可能耗时几分钟）。

    将 host workspace 挂载到 /workspace（使 sandbox 工具能读 PDF/DOCX）。
    失败时打印 docker stderr 供排查（常见：WSL2 docker-credential-desktop.exe /
    网络拉镜像失败 / 端口冲突）。
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
            capture_output=True, text=True, timeout=600,  # 预留 10 分钟拉镜像
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            log(f"[sandbox] docker run 失败（exit={r.returncode}）：\n  {stderr}")
            # 友好提示：检测 WSL2 凭据助手问题
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
        log(f"[sandbox] docker run 异常：{type(e).__name__}: {e}")
        return False


def _check_prerequisites(log=print) -> tuple["RuntimeSettings | None", str]:
    """公共前置检查：自启开关 + docker 可用性。

    返回 (settings, status)：
    - (settings, "ok")         检查通过，可继续
    - (None, "disabled")       用户禁用了自启
    - (None, "no_docker")      未检测到 docker
    """
    settings = get_settings()
    if not settings.lab_autostart_sandbox:
        return None, "disabled"
    if not _docker_available():
        return None, "no_docker"
    return settings, "ok"


def ensure_sandbox(log=print) -> bool:
    """按需检测并启动沙箱容器；返回端口最终是否可达。"""
    settings, status = _check_prerequisites(log)
    if status == "disabled":
        return True  # 用户禁用自启；交由 mcp_client 探活时报错
    if status == "no_docker":
        log("[sandbox] 未检测到 docker；请先装 docker 或手动起容器。")
        return False

    url = settings.aio_sandbox_mcp_url
    image = settings.aio_sandbox_image
    port = settings.aio_sandbox_port
    host_workspace = settings.workspace_dir
    host_workspace.mkdir(parents=True, exist_ok=True)

    # 一次性迁移提示：无 workspace 挂载的旧容器意味着 agent 永远无法在沙箱读 PDF
    if not _container_has_workspace_mount(host_workspace):
        log(
            "[sandbox] ⚠️ 检测到旧容器没有 workspace bind-mount。"
            f"sandbox 工具将无法读 {host_workspace} 下的 PDF/DOCX。\n"
            "  请运行：  docker rm -f aio-sandbox\n"
            "  然后重启 python -m server（会自动用新挂载重建容器）。"
        )
        # 继续：旧容器仍可跑，只是读不了文件；由用户决定是否重建。

    if probe_port(url):
        return True

    state = _container_state()
    if state == "running":
        # 容器 running 但端口未通；等待（健康检查可能尚未通过）
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
    """docker rm -f aio-sandbox；容器不存在也视为成功。"""
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True
        # 容器已不存在 -> 视为成功
        if "No such container" in (r.stderr or ""):
            return True
        log(f"[sandbox] docker rm 失败，stderr：{(r.stderr or '').strip()}")
        return False
    except Exception as e:
        log(f"[sandbox] docker rm 异常：{type(e).__name__}: {e}")
        return False


def recreate_sandbox(log=print) -> bool:
    """删除现有容器 + 重置 MCP/sandbox_tools 缓存 + 重启。

    目的：`/done --clear` 不仅清 host workspace，还清容器内的
    pip 全局包 / /tmp / 长驻进程残留，使下一个任务从干净容器起步。
    """
    settings, status = _check_prerequisites(log)
    if status == "disabled":
        log("[sandbox] LAB_AUTOSTART_SANDBOX=false，跳过重建（请手动 docker rm 后重启 cli）")
        return True
    if status == "no_docker":
        log("[sandbox] 未检测到 docker，跳过重建")
        return False

    if _container_state() is not None:
        if _docker_rm(log=log):
            log(f"[sandbox] {CONTAINER_NAME} 已删除")
        else:
            log(f"[sandbox] docker rm {CONTAINER_NAME} 失败；继续尝试重建")

    # 重置 MCP / sandbox 缓存（容器已变）
    try:
        from mcp_client import reset_mcp_client
        reset_mcp_client()
    except Exception as e:
        log(f"[sandbox] reset_mcp_client 失败（继续）：{type(e).__name__}: {e}")
    try:
        from tools.sandbox_tools import reset_sandbox_failure_counter
        reset_sandbox_failure_counter()
    except Exception as e:
        log(f"[sandbox] reset sandbox failures 失败（继续）：{type(e).__name__}: {e}")

    # 重启（等待端口就绪最多 60s）
    return ensure_sandbox(log=log)
