# Challenge 接入 SOP

> 给负责收集 / 打包 challenge 的同学。读完这一篇就够开工,**不需要**把整个 codebase 看一遍。
>
> 想要的最终交付物:**一个 `challenges/<name>/` 目录**,把它丢进 PR / zip 给我们,直接就能 `python -m main start` 跑起来。

## 0. 先看一遍参考 challenge

**所有正式 challenge 必须包含完整的 attack + defense 两个阶段。** 你要交付的形状参照:

| 在哪 | 看什么 |
|---|---|
| [`challenges/nautilus_ico/`](../challenges/nautilus_ico/) | **正式参考** — 2025 DEF CON Finals 真实题目 `nautilus-institute/finals-2025 :: ico`。二进制服务 + flag 在文件 + agent SSH 进 own_prod 改 Pascal 源码 + MCP `restart_service` 重新编译 + checker 验功能。完整的 attack/defense workflow,你的 challenge 应该长得跟它结构一样。 |
| [`challenges/notes/`](../challenges/notes/) | 框架烟雾测试,**不是参考实现**。它没有 defense 阶段,只用来跑通框架基本路径。**不要照它的形状交付你的 challenge**。|

90% 的问题答案在 `nautilus_ico/` 里。开始之前先把它完整读一遍。

## 1. 一个 challenge 必须提供 5 样东西

```
Challenge = (vulbox, functionality_test, flag_handler, restart_handler, docs)
```

外部贡献的 challenge **5 个组件全部必填**:

| 组件 | 是什么 |
|---|---|
| **vulbox** | docker image 引用 + 暴露的端口 |
| **functionality_test** | 怎么判断服务还活着 / 能不能正常用(4 级 health probe) |
| **flag_handler** | 怎么往服务里塞 flag,以及怎么读回来 |
| **restart_handler** | agent 修改源码后怎么重新编译 / 重启服务 — **defense 阶段的核心** |
| **docs** | 给 agent 看的 attack + defense 提示词 + 给人看的简介 |

> 框架本身允许 `restart_handler = None`(用 ABC 默认值),但**这是给内部测试用的特殊路径**。
> 外部 challenge **必须** override 它,否则 PR 会被打回去。

## 2. Defense 流程是什么样

每个外部 challenge 都必须支持这套交互(参考 ico):

1. 框架按节奏触发 `DEFENSE_KICKOFF`
2. Agent SSH 进 own_prod(以 `arena_agent` 身份)
3. Agent **修改 `/srv/challenge/<name>/` 里的源码** — 这个目录是你的 image 在 entrypoint 时从 `/opt/challenge_src/<name>/` 复制出来的可写副本
4. Agent 通过 MCP 调 `restart_service` → 框架走你的 `restart_handler.run(target)` → 你的 `/arena/restart.sh` 重新 build + spawn 服务
5. Framework 跑 `functionality_test` 确认服务还能用(CHECKER 那一层必须 pass — 不然 agent 把功能破坏了,该轮不算分)
6. Agent 通过 MCP 调 `report_deploy_complete` 告诉框架结束

**关键设计点:**
- **源码在 image 里 baked**(`/opt/challenge_src/<name>/` 是 read-only 模板)。每个容器 entrypoint 把它 cp 到 `/srv/challenge/<name>/` 作为该容器的可写副本 — agent 改的是这一份,不影响其他 team。
- **`/arena/restart.sh` 是 root-owned 555 的** —— agent 能读能执行,**不能**改。修改源码 / 触发 build,只能通过你定义的"改源码 → restart_service"接口。
- **build toolchain 留在 runtime image 里**(比如 ico 的 image 里始终有 fpc)— 必须能在容器内重新编译。
- **CHECKER 是真的 gameplay-correctness 探测**,不是 TCP 通就行。Agent 把功能改坏了,CHECKER 必须挂掉。

## 3. 目录结构

照着抄就行,文件名固定:

```
challenges/<your-name>/
├── __init__.py                # 导出你的 Challenge 类
├── challenge.py               # composition root,组合上面 5 个东西
├── functionality_test.py      # 实现 FunctionalityTest
├── flag_handler.py            # 实现 FlagHandler
├── restart_handler.py         # 实现 RestartHandler(必须 — defense 流程)
├── prompts.py                 # build_attack_prompt + build_defense_prompt(都必须)
├── build.py                   # 怎么 docker build 这个 challenge 的 image
└── image/                     # docker 构建上下文
    ├── Dockerfile
    ├── entrypoint.sh
    ├── restart.sh             # 框架调用的重新编译 + 重启脚本(必须)
    ├── checker.sh             # 给 functionality_test 的 CHECKER 层用(推荐)
    └── <服务源码 / 二进制 / 任何 image 需要 COPY 进去的文件>
```

命名约定:
- **目录名**:`<challenge-name>` 用下划线(python module),例如 `nautilus_ico`
- **`Challenge.name`**:kebab-case 字符串,与 `game.yaml` 里的引用对应,例如 `"nautilus-ico"`
- **image tag**:`cyberarena/chal-<name>:<version>`,例如 `cyberarena/chal-nautilus-ico:366520c0cc43`

## 4. 你拿到一个新 challenge 后的打包流程

### 步骤 1 — 把原 challenge 跑起来 + 摸清楚 defense surface

先**不用动框架**,在你自己机器上把这个 vulnerable app docker run 起来,确认:

- [ ] 服务监听某个 TCP 端口
- [ ] 知道 flag 在哪 — 是文件?是协议里的某个字段?
- [ ] 服务起来后你能用 nc / curl / python socket 跟它说一句话,确认它能响应
- [ ] **能从源码 build 出可运行的 binary** — defense 阶段 agent 改源码后,你的 `restart.sh` 必须能在容器内重新编译。先用手动跑通这一步:`docker exec <container> <build command>` 出来的二进制能正常启动 + 响应请求
- [ ] **能识别"功能正确性"的标志** — agent 改完源码后,我们怎么判断它没把服务改坏?(对 ico 来说就是 CONNECT byte handshake 还能 ACK;每个 challenge 都得有这种"功能没破"的探测点)
- [ ] **能识别 2-3 个可信的 vuln hotspot** — defense prompt 要告诉 agent 改哪里。光说"这个服务有漏洞,你看着办"是不够的

如果以上任一项搞不定,**先停**,带着原始资料(github / writeup / Dockerfile)找我们对一下。**特别是后两条** — 没有可验证的"功能正确性"探测点 + 没有可指认的 vuln hotspot,这个 challenge 本身不适合做 defense 流程,需要从源头换一个。

### 步骤 2 — 写 `image/`

复制 [`challenges/notes/image/`](../challenges/notes/image/) 或 [`challenges/nautilus_ico/image/`](../challenges/nautilus_ico/image/) 整个目录作为模板,改成你自己的:

#### Dockerfile **必须**满足

1. `FROM --platform=linux/amd64 ubuntu:24.04`(或类似 linux/amd64 base — 我们的 toolchain 锁 amd64)
2. 装好运行 challenge 需要的所有东西(`apt-get install ...`)
3. **创建 `arena_agent` 用户** —— agent 用这个非 root 用户 SSH 进来
   ```dockerfile
   RUN useradd --create-home --shell /bin/bash arena_agent
   ```
4. **装 openssh-server,改 sshd 配置**禁掉 root 登录
   ```dockerfile
   RUN apt-get install -y openssh-server \
       && mkdir -p /run/sshd /home/arena_agent/.ssh \
       && chown arena_agent:arena_agent /home/arena_agent/.ssh \
       && chmod 700 /home/arena_agent/.ssh \
       && sed -i 's/^#*PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
   ```
5. **bake 服务源码**:`COPY src/ /opt/challenge_src/<name>/`,然后 `RUN chmod -R 555 /opt/challenge_src/<name>`(只读模板)。entrypoint 起来后 cp 到 `/srv/challenge/<name>/` 作为 per-container 可写副本,chown 给 arena_agent。
6. **bake framework 脚本**到 `/arena/`,root:root 555:
   ```dockerfile
   COPY restart.sh /arena/restart.sh
   COPY checker.sh /arena/checker.sh
   RUN chmod 555 /arena/restart.sh /arena/checker.sh \
       && chown root:root /arena/restart.sh /arena/checker.sh
   ```
   agent 能读能执行,**不能**改 — 这是 defense 流程的安全边界,别破坏。
7. **预编译初始二进制**(如果服务是编译型的,比如 ico 的 Pascal / C / Rust)— Dockerfile build 阶段就 build 一次,产物放在 `/opt/challenge_src/<name>/` 模板里,entrypoint cp 时一起带过去。理由:启动时不再做工作 + 同 image 产 binary 是确定的(reproducibility)。
8. EXPOSE 22(SSH 必须)+ 你的服务端口

#### entrypoint.sh **必须**做的事

```bash
#!/usr/bin/env bash
set -e

# 1. sshd host keys(首次启动用)
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi
mkdir -p /run/sshd

# 2. materialize per-container writable workdir(必须 — defense 流程靠它)
mkdir -p /srv/challenge/<name>
cp -r /opt/challenge_src/<name>/. /srv/challenge/<name>/
chown -R arena_agent:arena_agent /srv/challenge/<name>
chmod -R u+w /srv/challenge/<name>

# 3. 启动 challenge 服务
#   - 编译型服务(ico 风格):服务通常 NOT 在 entrypoint 启动,而是
#     交给 /arena/restart.sh — 框架会在 deploy 后立刻通过 MCP 触发它。
#     entrypoint 这一段就空着(或者直接走 exec sshd)。
#   - 解释型服务(notes 风格):entrypoint 自己 nohup 起。
#
#     nohup <你的服务启动命令> > /var/log/<name>.stdout 2>&1 &

# 4. exec sshd in foreground, PID 1 reap zombies
exec /usr/sbin/sshd -D -e
```

**关键原则:** image 里**不要**留任何"启动时下载 / 编译"的步骤。源码和二进制都打进 image,启动只做 ssh keygen + cp + exec sshd。理由见 [`docs/UML/challenges.md`](UML/challenges.md)。

### 步骤 3 — 写 `build.py`

照抄 [`challenges/notes/build.py`](../challenges/notes/build.py)。基本就是把你的 `image/` 作为 docker build context,tag 设成 `cyberarena/chal-<name>:<version>`。

### 步骤 4 — 写 `flag_handler.py`

选一种 flag 投放方式,看哪种最适合你的 challenge:

| Flag 形式 | 例子 | plant 实现 | retrieve 实现 |
|---|---|---|---|
| **服务自身协议** | notes(PUT/GET/LIST) | 通过 docker exec + 服务的协议(如 socket 发 PUT) | 通过协议读回 |
| **文件落盘**(FILE_AT_PATH) | ico(`/flag`) | docker exec `printf %s "$flag" > /flag && chmod 644 /flag` | docker exec `cat /flag` |
| **环境变量** | (无现成例子) | docker exec write 到某个文件 / 触发 reload | 服务自己的 API 读 |
| **数据库行 / NoSQL key** | 用 web 服务 | 调一个内部 API insert | 调 GET |

照 [`challenges/notes/flag_handler.py`](../challenges/notes/flag_handler.py) 或 [`challenges/nautilus_ico/flag_handler.py`](../challenges/nautilus_ico/flag_handler.py) 实现两个方法:

```python
class YourFlagHandler(FlagHandler):
    @property
    def name(self) -> str: return "<your-name>-flag-handler"

    def plant(self, target: VulboxTarget, flag: str) -> str:
        """把 flag 放进 target.host。返回一个 handle(随便什么 string,
        比如 flag 在哪个 path,或者数据库的 row id)— 后续 retrieve 用它。"""
        exec_in = target.meta["exec_in_container"]
        # 用 exec_in(target.host, "...some shell command...")
        # rc 非 0 时 raise RuntimeError(...)
        ...

    def retrieve(self, target: VulboxTarget, handle: str) -> str | None:
        """把 plant 时放进去的 flag 读回来。读不到返回 None,不要 raise。"""
        ...
```

### 步骤 5 — 写 `functionality_test.py`

四级 health probe 是推荐约定(panel 知道怎么渲染它们),自顶向下:

| 层级 | 检查什么 | 实现思路 |
|---|---|---|
| `process` | 服务进程在不在 | `pgrep -f <binary path>` |
| `tcp` | 端口接不接受连接 | `python3 -c "socket().connect(...)"` |
| `protocol` | 能不能跟服务说一句话 | 发个 hello,看回应 |
| `checker` | 服务功能是不是正确(比如 flag 还能读取) | 跑一个完整的 happy-path interaction |

参考 [`challenges/nautilus_ico/functionality_test.py`](../challenges/nautilus_ico/functionality_test.py) 的四层结构。如果某些层不适用(比如 notes 的 protocol 和 checker 是一回事),让一个 alias 到另一个,不要硬凑。

```python
class YourFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str: return "<your-name>-functionality"
    @property
    def required_ports(self) -> tuple[str, ...]: return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        # 跑你的检查,组装一棵 CheckResult 树:
        # CheckResult(name="<your-name>", passed=all_pass, children=[
        #     CheckResult(name="process", passed=..., detail=...),
        #     CheckResult(name="tcp",     passed=..., detail=...),
        #     CheckResult(name="protocol",passed=..., detail=...),
        #     CheckResult(name="checker", passed=..., detail=...),
        # ])
        ...
```

### 步骤 6 — 写 `restart_handler.py` + image 里的 `/arena/restart.sh`(必须)

Defense 流程的核心。两个文件配合:

**`image/restart.sh`** — root-owned 555,通过 `docker exec` 被框架调用:

```bash
#!/usr/bin/env bash
set -e

SRC_DIR="/srv/challenge/<name>"
BIN="${SRC_DIR}/<binary>"
LOG="/var/log/<name>.stdout"

cd "${SRC_DIR}"

# 1. 重新编译 — 同 image 用同样的 flags(reproducibility)
<你的 build command,比如 gcc / fpc / cargo build / npm run build>

# 2. 杀掉旧实例(首次没有也 OK)
pkill -f "${BIN}" || true

# 3. 后台启动新二进制
mkdir -p "$(dirname "${LOG}")"
nohup "${BIN}" > "${LOG}" 2>&1 &

# 4. 给个短暂的稳定时间,然后让 health probe 接管
sleep 0.5

echo "<name> restarted, pid=$(pgrep -f "${BIN}" | head -1)"
```

**`restart_handler.py`** — 调上面的脚本,返回 CheckResult 树。一定要分 leaf:

| leaf | 检查什么 |
|---|---|
| `compile` | `/arena/restart.sh` 退出码 0(编译成功) |
| `spawn` | 新进程 pgrep 得到 |
| `smoke` | TCP probe 你的端口能接 |

如果 compile 挂了,smoke 应该明确报"skipped — restart.sh failed",别再去 probe 一个已经死掉的端口。

模板:[`challenges/nautilus_ico/restart_handler.py`](../challenges/nautilus_ico/restart_handler.py) +
[`challenges/nautilus_ico/image/restart.sh`](../challenges/nautilus_ico/image/restart.sh)。**这一步不能省 — 没有 defense 流程的 challenge PR 会被打回去。**

### 步骤 7 — 写 `prompts.py`(两个 prompt 都必须)

两个函数,**都必须实现**。看 [`challenges/nautilus_ico/prompts.py`](../challenges/nautilus_ico/prompts.py) 的完整版:

```python
from agents.prompts._helpers import submit_block

def build_attack_prompt(cfg: AgentConfig) -> str:
    """攻击 agent 看到的提示词。包含:
      - 网络拓扑(怎么连到 opposing prod)
      - 服务协议简介 / 已知 attack surface
      - submit flag 的方式(submit_block(cfg) 自动生成)
    """
    return f"""...{submit_block(cfg)}..."""


def build_defense_prompt(cfg, *, service_name, port, source_dir="/srv/challenge") -> str:
    """防御 agent 看到的提示词。必须包含:
      - 警告:DEFENSE 阶段提交 flag 会被服务器拒(reason=WRONG_PHASE)
      - SSH 到 own_prod 的入口(arena_agent)
      - 源码在哪(/srv/challenge/<name>/)+ 目录布局简介
      - 已知 vuln hotspots(2-3 个具体文件 / 函数,不能只说"看着办")
      - 触发 rebuild 的方式(mcp__cyberarena__restart_service)
      - 等服务回来 + report_deploy_complete
    """
    ...
```

**两条硬约束:**
1. **defense prompt 必须包含 "DEFENSE 阶段不能 submit flag" 的明确告知** — 见 ico 的模板。这条违规会被 server 直接拒掉(reason=WRONG_PHASE),提前告诉 agent 也省 token。
2. **vuln hotspots 必须具体到文件 / 函数级别** — 你是出题人,你知道漏洞在哪。agent 是攻防执行者,不是审计专家。模糊的"自己找找看"等于浪费 attack 预算。

### 步骤 8 — 写 `challenge.py`(composition root)

把上面 5 个组件穿起来。模板:

```python
"""<YourName>Challenge — composition root."""
from __future__ import annotations
from functools import cached_property
from pathlib import Path

from challenges.interface import (
    Challenge, ChallengeDocs, FlagHandler, FunctionalityTest,
    RestartHandler, VulboxImage,
)
from .flag_handler import YourFlagHandler
from .functionality_test import YourFunctionalityTest
from .restart_handler import YourRestartHandler

_VULBOX = VulboxImage(
    reference="cyberarena/chal-<name>:v1",
    exposed_ports={"service": <你的端口>},
    build_context=str(Path(__file__).resolve().parent / "image"),
)

_DOCS = ChallengeDocs(
    intro="一句话给人看的服务介绍 + vuln 来源",
    agent_prompts={},        # 留空,prompts.py 在另一个 dispatch 链路
    func_test_spec=(
        "PROCESS  — ...\n"
        "TCP      — ...\n"
        "PROTOCOL — ...\n"
        "CHECKER  — ...\n"
    ),
)


class YourChallenge(Challenge):
    @property
    def name(self) -> str: return "<your-kebab-name>"

    @cached_property
    def vulbox(self) -> VulboxImage: return _VULBOX

    @cached_property
    def functionality_test(self) -> FunctionalityTest:
        return YourFunctionalityTest()

    @cached_property
    def flag_handler(self) -> FlagHandler:
        return YourFlagHandler()

    @cached_property
    def restart_handler(self) -> RestartHandler | None:
        return YourRestartHandler()

    @cached_property
    def docs(self) -> ChallengeDocs: return _DOCS
```

### 步骤 9 — `__init__.py` 一行导出

```python
from .challenge import YourChallenge
__all__ = ["YourChallenge"]
```

### 步骤 10 — 在 registry 注册(我们这边帮你做)

把你的目录交给我们,我们在 [`challenges/registry.py`](../challenges/registry.py) 加 4 行:

```python
_CHALLENGE_FACTORIES["<your-kebab-name>"] = YourChallenge
_IMAGE_BUILDERS["<your-kebab-name>"]      = _your_build         # 调你的 build.py
# 下面两条只有需要时加
_INITIAL_START["<your-kebab-name>"]       = <hook>              # 启动后才需要做的事
_HEALTH_INTERVAL_SECS["<your-kebab-name>"] = 30                 # health 轮询间隔
```

如果你能 PR 顺手把这 4 行也改了更好,但不强求 — 你的 `challenges/<name>/` 目录本身是自包含的。

## 5. 接收前的 self-check checklist

在交付前自己跑过这些,大幅减少返工:

**Build / startup**
- [ ] `image/` 能 `docker build` 出来,**不**报错,产出 `cyberarena/chal-<name>:<version>` tag
- [ ] `docker run --rm cyberarena/chal-<name>:<version>` 起来后,**5 秒内**能用 `nc localhost <port>` 或 `python3 -c "socket()..."` 跟服务通话
- [ ] `docker exec <container> id arena_agent` 返回 uid > 0
- [ ] `docker exec <container> sshd -t` 不报错(sshd 配置合法)

**Service health**
- [ ] `docker exec <container> pgrep <服务进程名>` 返回非空(若服务由 restart.sh 启动,先跑一次它)
- [ ] flag 投放手测:`docker exec <container>` 跑你 plant 的 shell 命令,然后跑 retrieve 命令,**取回来的串和投放的一致**

**Defense workflow** (这一段全部必须过)
- [ ] `docker exec <container> /arena/restart.sh` 退出码 0,新进程 pgrep 能匹到,端口能 connect
- [ ] 故意改坏源码(比如把一行核心逻辑注释掉),`/arena/restart.sh` 仍然能**重新编译成功**(说明 toolchain 完整)
- [ ] 故意改坏源码到编译都过不了(比如删一个分号),`/arena/restart.sh` **必须退出码非 0**(说明 build error 能传出来)
- [ ] 你 functionality_test 的 CHECKER 那一层,在**正常服务**下 pass,在**功能被破坏的服务**(比如 agent 改坏了响应格式)下挂掉 — 不能只判断端口接不接

**框架卫生**
- [ ] `challenges/<name>/` 整个目录里没有任何**绝对路径 hardcode**(包括 user home / /tmp 之类的全局 path,除非是 image 内的固定位置)
- [ ] image **不需要** internet 才能起来(no `apt-get update` / `pip install` 在 entrypoint 里)
- [ ] `from challenges.<name> import <Name>Challenge` 在 python 里能 import 不报错
- [ ] `<Name>Challenge().restart_handler` 返回 RestartHandler 实例(**不是 None**)

## 6. 完整目录树参考

最终交付应该长这样(以一个假设的 `myctf` 为例):

```
challenges/myctf/
├── __init__.py            (~3 行)
├── challenge.py           (~50 行)
├── functionality_test.py  (~50-80 行)
├── flag_handler.py        (~40-80 行)
├── restart_handler.py     (~60 行)
├── prompts.py             (~80-200 行)
├── build.py               (~40 行)
└── image/
    ├── Dockerfile         (~50 行)
    ├── entrypoint.sh      (~20 行)
    ├── restart.sh         (~30 行)
    ├── checker.sh         (~30 行,推荐)
    └── service/           (你的服务源码 — 源文件 / 编译产物)
```

`nautilus_ico/` 的文件数量 + 行数大致就是这个量级,可以直接对照。

## 7. 常见坑

| 坑 | 怎么躲 |
|---|---|
| 忘了创建 `arena_agent` 用户 | 框架在 install agent 时会探测,有 fallback 到 root,但**警告 log**,会被 reviewer 卡 |
| 没禁 `PermitRootLogin` | 同上 — 安全 review 卡 |
| 服务用 `localhost` 而不是 `0.0.0.0` 绑定 | 容器内 health probe 走 `127.0.0.1` 没问题,但 cross-team attack 走 `0.0.0.0` 会失败 |
| entrypoint 里跑 `apt-get install` / 远程下载 | 网络抖一下全场崩;所有 install 必须在 Dockerfile build 阶段做完 |
| flag 直接打进 image 里 | flag **每轮 rotate**,build 时根本不知道值。plant 是 runtime 行为,不是 build-time |
| 服务起来太慢(几秒以上) | functionality_test 在 deploy 后立刻跑,慢启动 = 假阴性。要么 entrypoint 阻塞到 ready,要么 functionality_test 自己 retry 几次 |
| Dockerfile 不锁 `--platform=linux/amd64` | arm Mac 上 build 出 arm 镜像,EC2 amd64 上 pull 失败 |
| `restart_handler` 留 None / 没写 defense 流程 | 框架不会报错(ABC 允许 None),但 PR review **必然打回** — 外部贡献必须包含完整 defense |
| CHECKER 只检测 "端口接" | 等于没检测 — agent 把响应改成 "OK" 也能过。CHECKER 必须验证**功能正确性**,例如完整的请求 / 响应 round-trip |
| defense prompt 没明确禁止 submit | agent 可能在 DEFENSE 阶段误调 submit_flag,虽然 server 会拒(WRONG_PHASE)但浪费 token + 误导调试 |
| `/srv/challenge/<name>/` 没在 entrypoint cp | agent SSH 进来发现没源码可改,defense 流程崩 |
| 编译型服务忘了把 toolchain 装进 runtime image | restart.sh 在容器内 build 不出来,defense 永远失败 |

## 8. 出问题怎么调

按这个顺序排查:

1. `docker logs <container>` — entrypoint / 服务起没起
2. `docker exec <container> bash` 进去看 — 文件在不在,权限对不对
3. `docker exec <container> ss -tlnp` — 监听端口对不对
4. 跑 `python -c "from challenges.<your_name> import YourChallenge; c = YourChallenge(); print(c.functionality_test.run(...))"` — 在 Python 里直接调你的 functionality_test,看返回的 CheckResult 树
5. 实在不行,找我们,**带上**:
   - 你的整个 `challenges/<name>/` 目录
   - `docker logs` 输出
   - 你跑过的 self-check 里哪一步过不了

## 9. 提交流程

1. fork repo / 在 feature branch 上工作
2. 你的 `challenges/<name>/` 目录是**唯一**新增的代码 — 不要改框架其他文件
3. PR description 里附上:
   - challenge 来源(github / CTF 比赛 / 自研)
   - flag 投放方式
   - **defense workflow 描述** — agent 应该改哪些文件 / 函数,vuln 是什么类
   - self-check checklist **全部**过了的证据(截图 / 命令输出)
4. 我们 review + 在 `challenges/registry.py` 加 4 行 + 合并

---

**附:核心接口的源码**

如果你想看精确签名:[`challenges/interface/challenge.py`](../challenges/interface/challenge.py) + [`challenges/interface/probe.py`](../challenges/interface/probe.py)。这两个文件本身没有任何外部依赖,可以独立读。
