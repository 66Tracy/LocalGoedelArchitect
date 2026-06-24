# Kimina Lean Server 离线镜像构建与部署指南

本文档说明如何将 `kimina-lean-server` 打包为完全离线的 Docker 镜像，部署到无公网访问的内部服务器上。

---

## 一、构建思路

### 1.1 为什么要做离线镜像

kimina-lean-server 运行时依赖以下组件，它们在首次启动或运行过程中会尝试联网：

| 组件 | 联网行为 | 离线处理方式 |
|---|---|---|
| **elan** | 安装时下载 Lean 工具链；运行时会检查更新 | 构建时预装工具链；运行时禁用更新检查 |
| **lake** | `lake build` 时从 git 拉取依赖；`lake exe cache get` 下载编译缓存 | 构建时完成所有编译；修改 manifest 为本地 path 类型 |
| **mathlib4** | 依赖众多 Lean 包，需从 GitHub 下载 | 构建时完整克隆并编译，预装入镜像 |
| **Python 依赖** | `pip install` 从 PyPI 下载 | 构建时全部安装到镜像中 |

### 1.2 核心策略：构建时联网，运行时零依赖

整个方案遵循"有网构建、无网运行"的原则：

1. **构建阶段**（在有网络的机器上）：安装 elan、下载 Lean 工具链、克隆 repl 和 mathlib4、编译所有依赖、安装 Python 包
2. **镜像封装**：将编译产物、缓存、依赖全部打入 Docker 镜像
3. **离线阶段**（内部服务器）：仅通过 `docker load` 加载镜像，无需任何外部网络

### 1.3 关键处理点

- **lake-manifest.json 改写**：构建完成后，将 mathlib4 的 `lake-manifest.json` 中所有包的 `type` 从 `"git"` 改为 `"path"`，并删除 `url` 字段。这样 lake 在运行时不会再尝试从 git 拉取依赖
- **elan 自更新禁用**：通过 `ELAN_NO_UPDATE_CHECK=1` 和 `elan config --disable-self-update` 双重保证
- **工具链预装**：构建时通过 `elan-init.sh --default-toolchain v4.26.0` 将工具链固定安装到镜像中

---

## 二、文件说明

### 2.1 `Dockerfile`（仓库根目录）

定义镜像的构建流程，关键步骤：

```dockerfile
# 基础镜像：Python 3.13 slim
FROM python:3.13-slim

# 构建参数：Lean 版本、REPL 源、mathlib 源等
ARG LEAN_SERVER_LEAN_VERSION=v4.26.0
ARG REPL_REPO_URL=https://github.com/leanprover-community/repl.git
ARG REPL_BRANCH=${LEAN_SERVER_LEAN_VERSION}
ARG MATHLIB_REPO_URL=https://github.com/leanprover-community/mathlib4.git
ARG MATHLIB_BRANCH=${LEAN_SERVER_LEAN_VERSION}

# 运行时环境变量
ENV LEAN_SERVER_LEAN_VERSION=${LEAN_SERVER_LEAN_VERSION} \
    LEAN_SERVER_REPL_PATH=/repl/.lake/build/bin/repl \
    LEAN_SERVER_PROJECT_DIR=/mathlib4 \
    LEAN_SERVER_MAX_REPL_MEM=12G \          # 关键：内存限制（见下文注意事项）
    LEAN_SERVER_INIT_REPLS='{"import Mathlib": 1}' \
    ELAN_NO_UPDATE_CHECK=1                   # 禁用 elan 更新检查

# 1. 安装系统依赖（curl, git, jq, build-essential 等）
RUN apt-get update && apt-get install -y ...

# 2. 复制并执行 setup.sh（安装 elan、lean、repl、mathlib4）
COPY setup.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/setup.sh \   # 修复 Windows CRLF
 && chmod +x /usr/local/bin/setup.sh \
 && /usr/local/bin/setup.sh

# 3. 安装 Python 依赖
RUN pip install ... && prisma generate

# 4. 启动服务
CMD ["python", "-m", "server"]
```

### 2.2 `setup.sh`（仓库根目录）

在容器构建阶段执行，完成 Lean 生态的预装：

```bash
#!/usr/bin/env bash
set -euxo pipefail

# 安装 elan（Lean 工具链管理器）
curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf \
  | sh -s -- --default-toolchain "${LEAN_SERVER_LEAN_VERSION}" -y

# 禁用 elan 自动更新（离线环境必须）
elan config --disable-self-update

# 克隆并编译 REPL
install_repo repl "$REPL_REPO_URL" "$REPL_BRANCH" false

# 克隆并编译 mathlib4
install_repo mathlib4 "$MATHLIB_REPO_URL" "$MATHLIB_BRANCH" true
```

`install_repo` 函数的核心逻辑：

1. `git clone --single-branch --depth 1` 浅克隆仓库
2. `lake exe cache get` 下载 mathlib4 的预编译缓存（加速构建，失败则回退到源码编译）
3. `lake build` 编译项目
4. 对 mathlib4 执行 **manifest 改写**：
   ```bash
   jq '.packages |= map(.type="path"|del(.url)|.dir=".lake/packages/"+.name)' \
      lake-manifest.json
   ```
5. 构建完成后删除 `.git` 目录减小镜像体积

### 2.3 `deploy/offline/build-and-save.sh`

一键构建脚本，执行以下流程：

```
构建镜像
    |
    v
启动容器（--network=none）进行离线冒烟测试
    |
    v
等待 "Initialized REPLs" 日志（最多 10 分钟）
    |
    v
测试通过 → docker save | gzip 导出 tar.gz
```

可覆盖的环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LEAN_VERSION` | `v4.26.0` | Lean 版本 |
| `IMAGE` | `kimina-lean-server:${LEAN_VERSION}-offline` | 输出镜像名 |
| `PLATFORM` | `linux/amd64` | 目标平台架构 |
| `APT_MIRROR` | "" | APT 源镜像（国内网络可设阿里云） |
| `PIP_INDEX_URL` | "" | pip 源镜像 |
| `SKIP_UV` | "" | 非空则跳过 uv 安装 |

### 2.4 `deploy/offline/compose.offline.yaml`

离线服务器上的 Docker Compose 配置：

```yaml
services:
  server:
    image: kimina-lean-server:v4.26.0-offline
    ports:
      - "80:8000"
    environment:
      LEAN_SERVER_ENVIRONMENT: prod
      LEAN_SERVER_LEAN_VERSION: v4.26.0
      LEAN_SERVER_MAX_REPL_MEM: 12G       # 与 Dockerfile 保持一致
      LEAN_SERVER_INIT_REPLS: '{"import Mathlib": 1}'
    restart: unless-stopped
```

---

## 三、构建步骤

### 3.1 前置要求

- 一台**有公网访问**的 Linux 机器（架构需与目标服务器一致，通常为 `linux/amd64`）
- Docker 已安装并运行
- Bash、Git 可用

### 3.2 执行构建

```bash
cd deploy/offline
bash build-and-save.sh
```

构建耗时约 10-30 分钟，取决于网络速度。成功后会输出：

```
==> Offline smoke test PASSED
...
==> Done.
  image:        kimina-lean-server:v4.26.0-offline
  image digest: sha256:...
  tar:          .../deploy/offline/out/kimina-lean-server-v4.26.0.tar.gz
  tar sha256:   8e5d3edcc18ace92aa7252329e0a06a24d1b1506b3263042b0138e56de74761f
```

### 3.3 使用国内镜像加速（可选）

如果构建机器在国内，可设置镜像源：

```bash
export APT_MIRROR=https://mirrors.aliyun.com
export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
export SKIP_UV=1
bash build-and-save.sh
```

---

## 四、部署步骤

### 4.1 传输文件到内部服务器

将以下两个文件复制到离线服务器：

```
deploy/offline/out/kimina-lean-server-v4.26.0.tar.gz
deploy/offline/compose.offline.yaml
```

### 4.2 加载镜像

在内部服务器上执行：

```bash
# 加载镜像（约 1-3 分钟，取决于磁盘速度）
gunzip -c kimina-lean-server-v4.26.0.tar.gz | docker load

# 验证镜像已加载
docker images | grep kimina-lean-server
```

### 4.3 启动服务

```bash
docker compose -f compose.offline.yaml up -d
```

服务将在后台运行，监听容器的 8000 端口，映射到宿主机的 80 端口。

### 4.4 查看日志

```bash
docker compose -f compose.offline.yaml logs -f
```

预期输出：

```
INFO  Running Kimina Lean Server 'v2.0.0' in prod mode with Lean version: 'v4.26.0'
INFO  REPL manager initialized with: MAX_REPLS=20, MAX_REPL_USES=-1, MAX_REPL_MEM=12288 MB
INFO  Initialized REPLs with: {"import Mathlib": 1}
INFO  Application startup complete.
INFO  Uvicorn running on http://0.0.0.0:8000
```

---

## 五、测试方法

### 5.1 快速功能测试

服务启动后，在离线服务器上执行：

```bash
curl --request POST \
  --url http://localhost:8000/api/check \
  --header 'Content-Type: application/json' \
  --data '{
    "snippets": [
      {"id": "check-nat-test", "code": "#check Nat"}
    ]
  }'
```

预期返回（类似）：

```json
{
  "snippets": [
    {
      "id": "check-nat-test",
      "response": {
        "messages": [
          {
            "severity": "info",
            "pos": {"line": 1, "column": 0},
            "endPos": {"line": 1, "column": 6},
            "data": "Nat : Type"
          }
        ],
        "env": 0
      }
    }
  ]
}
```

### 5.2 离线环境验证

确认容器确实在断网环境下运行：

```bash
# 查看容器网络模式（应为默认 bridge，但无外网访问）
docker inspect <容器名> | grep -i network

# 进入容器内部测试网络连通性
docker exec -it <容器名> bash -c "curl -I https://github.com 2>&1"
# 预期：连接超时或无法解析（证明确实离线）
```

### 5.3 健康检查端点

```bash
curl http://localhost:8000/health
```

预期返回：

```json
{"status": "ok"}
```

---

## 六、注意事项

### 6.1 内存限制（重要）

Lean 4.26.0 + Mathlib 在 REPL 启动时需要大量虚拟内存。测试发现：

| 内存限制 | 结果 |
|---|---|
| 8G | REPL 启动失败（进程被内存限制阻塞） |
| 9G+ | 正常工作 |
| **12G**（推荐） | 稳定运行，留有余量 |

因此 Dockerfile 和 compose 文件中将默认值设为 `12G`。如果你的服务器内存紧张，可尝试降低到 9-10G，但不建议低于 9G。

### 6.2 架构一致性

构建机器的 CPU 架构必须与目标服务器一致。如果不一致，需使用 Docker 的交叉编译：

```bash
# 例如：在 Apple Silicon Mac 上构建 amd64 镜像
export PLATFORM=linux/amd64
bash build-and-save.sh
```

### 6.3 镜像体积

离线镜像体积较大（约 2-3GB），主要来源于：
- mathlib4 预编译产物和依赖包
- Lean 工具链
- REPL 二进制（约 200MB）

### 6.4 版本升级

如需升级 Lean 版本，修改以下位置后重新构建：

1. `Dockerfile` 中的 `LEAN_SERVER_LEAN_VERSION`
2. `compose.offline.yaml` 中的 `LEAN_SERVER_LEAN_VERSION`
3. 重新运行 `build-and-save.sh`

---

## 七、故障排查

| 现象 | 可能原因 | 解决方法 |
|---|---|---|
| 构建时 `env: 'bash\r': No such file or directory` | setup.sh 有 Windows CRLF 换行符 | Dockerfile 中已添加 `sed -i 's/\r$//'` 自动修复 |
| 离线启动时 REPL 返回空响应 | 内存限制过低 | 提高 `LEAN_SERVER_MAX_REPL_MEM` 到 12G |
| `lake exe cache get` 失败 | 网络问题或 lake 版本不兼容 | setup.sh 中已添加 `|| true` 容错，会回退到源码编译 |
| 容器启动后立刻退出 | REPL 初始化失败 | 查看 `docker logs` 中的错误信息 |
| elan 尝试联网更新 | 环境变量未生效 | 检查 `ELAN_NO_UPDATE_CHECK=1` 是否设置 |

---

## 八、多机负载均衡部署（可选）

如果你有多台内部服务器，可以在每台服务器上独立部署 kimina-lean-server，然后用其中一台服务器运行 Nginx 作为负载均衡器，将流量以轮询（Round-Robin）方式分发给各后端。

### 8.1 架构

```
客户端请求
    |
    v
Nginx (负载均衡服务器，监听 8000 端口)
    |-- 轮询 -->
        Backend Server A :8001  (kimina-lean-server)
        Backend Server B :8002  (kimina-lean-server)
        Backend Server C :8003  (kimina-lean-server)
```

### 8.2 可行性说明

kimina-lean-server 的 HTTP API 是**无状态**的：
- 不依赖 session、cookie 或跨请求共享状态
- 每个 `POST /api/check` 请求携带完整的 snippet，独立处理
- 健康检查端点 `/health` 已内置

因此，Nginx 轮询负载均衡在功能上**完全可行**。

**性能注意**：服务器内部有一个 REPL header 复用优化——如果两个相同 header（如都 `import Mathlib`）的请求打到同一台后端，第二请求会直接复用已加载的 REPL（秒级响应）。如果轮询到不同后端，则需要重新加载（5-15 秒）。如果你的工作负载中有大量重复的 header，建议使用 `ip_hash` 策略替代纯轮询。

### 8.3 后端服务器部署

在**每台**后端服务器上执行：

1. 加载离线镜像（与单机部署相同）
2. 使用 `compose.backend.yaml` 启动服务：

```bash
# 复制 compose.backend.yaml 到服务器，按需修改端口映射
docker compose -f compose.backend.yaml up -d
```

`compose.backend.yaml` 与 `compose.offline.yaml` 的区别：
- 端口映射使用内部端口（如 `8001:8000`），而非宿主机的 80 端口
- 其余配置完全相同

**注意**：每台后端服务器的宿主机端口需要不同（或确保 Nginx 能通过不同 IP 访问），以便 Nginx 区分后端实例。

### 8.4 负载均衡服务器部署 Nginx

在选定的负载均衡服务器上：

**方式一：直接安装 Nginx**

```bash
# 安装 Nginx（Debian/Ubuntu 示例）
sudo apt-get update && sudo apt-get install -y nginx

# 将 nginx.conf 复制为配置
sudo cp nginx.conf /etc/nginx/nginx.conf
sudo nginx -s reload
```

**方式二：Docker 运行 Nginx**

```bash
docker run -d \
  --name nginx-lb \
  -p 8000:8000 \
  -v $(pwd)/nginx.conf:/etc/nginx/nginx.conf:ro \
  nginx:alpine
```

### 8.5 Nginx 配置说明

`nginx.conf` 核心配置：

```nginx
upstream kimina_backend {
    # 默认 Round-Robin（轮询）
    server 192.168.1.101:8001;
    server 192.168.1.102:8002;
    server 192.168.1.103:8003;
}
```

**可选策略调整**：

| 策略 | 配置 | 适用场景 |
|---|---|---|
| 轮询（默认） | 无需额外配置 | 请求分布均匀，后端性能一致 |
| `least_conn` | 加一行 `least_conn;` | 后端性能不均，将请求发给最空闲的服务器 |
| `ip_hash` | 加一行 `ip_hash;` | 同一客户端IP固定打到同一后端，提升 REPL 复用率 |

**超时设置**：`/api/check` 可能耗时数秒（REPL 编译执行），因此代理超时设得较宽：

```nginx
proxy_connect_timeout 30s;
proxy_send_timeout    120s;
proxy_read_timeout    120s;
```

### 8.6 验证

在任意能访问负载均衡服务器的机器上测试：

```bash
# 1. 健康检查
curl http://<nginx-ip>:8000/health
# 预期：{"status": "ok"}

# 2. 功能测试
curl --request POST \
  --url http://<nginx-ip>:8000/api/check \
  --header 'Content-Type: application/json' \
  --data '{
    "snippets": [{"id": "lb-test", "code": "#check Nat"}]
  }'
# 预期：返回 Nat : Type

# 3. 轮询验证（多次发送请求，观察各后端日志）
for i in {1..6}; do
  curl -s -o /dev/null http://<nginx-ip>:8000/health
done
```

观察各后端服务器的容器日志（`docker logs <容器名>`），确认请求被均匀分发。

### 8.7 内存规划

每台后端服务器预启动一个 `import Mathlib` 的 REPL，约占用 12GB 虚拟内存。如有 N 台后端，总内存需求约为 N × 12GB。

| 后端数量 | 预估总内存 | 备注 |
|---|---|---|
| 1 | 12 GB | 单机部署 |
| 2 | 24 GB | 两台各 12GB |
| 3 | 36 GB | 三台各 12GB |

如果总内存紧张，可将部分后端的 `LEAN_SERVER_INIT_REPLS` 设为空（`'{}'`），依靠请求到达时按需创建 REPL。
