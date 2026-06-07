# Challenge 接入 SOP

> 给负责收集 / 打包 challenge 的同学。读完这一篇就够开工,**不需要**把整个 codebase 看一遍。
>
> 想要的最终交付物:**一个 `challenges/<name>/` 目录**,把它丢进 PR / zip 给我们,直接就能 `python -m main start` 跑起来。

## 0. 先看一遍现有的两个 challenge

| 类型 | 在哪 | 用来对照学什么 |
|---|---|---|
| 最简单 / attack-only | [`challenges/notes/`](../challenges/notes/) | 网络服务 + flag 通过 service 自己的协议读取 |
| 完整 / 有 defense | [`challenges/nautilus_ico/`](../challenges/nautilus_ico/) | 二进制服务 + flag 在文件 + agent 需要 patch 源码并重启 |

你要做的就是参照其中一个的形状,把你拿到的 vulnerable app 装进去。**任何疑问先看这两个,90% 的问题答案在里面**。

## 1. 一个 challenge 必须提供 5 样东西

```
Challenge = (vulbox, functionality_test, flag_handler, restart_handler?, docs)
```

| 组件 | 是什么 | 必须? |
|---|---|---|
| **vulbox** | 一个 docker image 引用 + 暴露的端口 | ✅ 必须 |
| **functionality_test** | 怎么判断服务还活着 / 能不能正常用 | ✅ 必须 |
| **flag_handler** | 怎么往服务里塞一个 flag,以及怎么读回来 | ✅ 必须 |
| **restart_handler** | agent 修改源码后怎么重启服务(defense 流程) | ⭕ 只有 defense-capable 的 challenge 需要;attack-only 的留 None |
| **docs** | 给 agent 看的攻击 / 防御提示词 + 给人看的简介 | ✅ 必须 |

## 2. 目录结构

照着抄就行,文件名固定:

```
challenges/<your-name>/
├── __init__.py                # 导出你的 Challenge 类
├── challenge.py               # composition root,组合上面 5 个东西
├── functionality_test.py      # 实现 FunctionalityTest
├── flag_handler.py            # 实现 FlagHandler
├── restart_handler.py         # 只有 defense-capable 的写;否则不要这个文件
├── prompts.py                 # build_attack_prompt + 可选 build_defense_prompt
├── build.py                   # 怎么 docker build 这个 challenge 的 image
└── image/                     # docker 构建上下文
    ├── Dockerfile
    ├── entrypoint.sh
    └── <服务源码 / 二进制 / 任何 image 需要 COPY 进去的文件>
```

命名约定:
- **目录名**:`<challenge-name>` 用下划线(python module),例如 `nautilus_ico`
- **`Challenge.name`**:kebab-case 字符串,与 `game.yaml` 里的引用对应,例如 `"nautilus-ico"`
- **image tag**:`cyberarena/chal-<name>:<version>`,例如 `cyberarena/chal-nautilus-ico:366520c0cc43`

## 3. 你拿到一个新 challenge 后的打包流程

### 步骤 1 — 把原 challenge 跑起来

先**不用动框架**,在你自己机器上把这个 vulnerable app docker run 起来,确认:

- [ ] 服务监听某个 TCP 端口(或者你能定义它怎么被访问)
- [ ] 知道 flag 在哪 — 是文件?是协议里的某个字段?
- [ ] 服务起来后你能用 nc / curl / python socket 跟它说一句话,确认它能响应

如果连这一步都搞不定,**先停**,带着原始的 challenge 资料(github / writeup / Dockerfile)来找我们对一下。

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
5. **defense-capable** 才需要的:把可被 agent 编辑的源码 COPY 到 `/srv/challenge/<name>/`,chown 给 arena_agent。Framework 脚本 `restart.sh` / `checker.sh` 放 `/arena/`,root:root 555。
6. EXPOSE 22(SSH 必须)+ 你的服务端口

#### entrypoint.sh **必须**做的事

```bash
#!/usr/bin/env bash
set -e

# 1. sshd host keys(首次启动用)
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A
fi
mkdir -p /run/sshd

# 2. (defense-capable only)materialize per-container writable workdir
# cp -r /opt/challenge_src/<name>/. /srv/challenge/<name>/
# chown -R arena_agent:arena_agent /srv/challenge/<name>/

# 3. 启动 challenge 服务(如果不是由 restart.sh 启动的)
nohup <你的服务启动命令> > /var/log/<name>.stdout 2>&1 &

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

### 步骤 6 — 写 `restart_handler.py`(**只有 defense-capable challenge 需要**)

如果你的 challenge 设计了"防御阶段 — agent 改源码再重启"的玩法,需要:

1. image 里有 `/arena/restart.sh`(你写)负责真正的重新编译 + 重启
2. `restart_handler.py` 通过 `docker exec /arena/restart.sh` 触发它,返回 CheckResult

模板:[`challenges/nautilus_ico/restart_handler.py`](../challenges/nautilus_ico/restart_handler.py)

**attack-only challenge** 完全跳过这一步。`challenge.py` 里也**不要** override `restart_handler` —— 用 ABC 默认的 `None`。

### 步骤 7 — 写 `prompts.py`

两个函数,看 [`challenges/nautilus_ico/prompts.py`](../challenges/nautilus_ico/prompts.py):

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
    """只 defense-capable 才写。包含:
      - 源码在哪 (/srv/challenge/<name>/)
      - 建议哪些文件是修补点(known vuln hotspots)
      - 怎么触发 rebuild(MCP restart_service)
    """
    ...
```

**重要:** defense prompt 一定要包含一段"DEFENSE 阶段不能 submit flag"的明确告知,见现有模板。这条违规会被 server 直接拒掉,告诉 agent 也省 token。

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
# from .restart_handler import YourRestartHandler   # 只 defense-capable

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

    # 只 defense-capable 才 override:
    # @cached_property
    # def restart_handler(self) -> RestartHandler | None:
    #     return YourRestartHandler()

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

## 4. 接收前的 self-check checklist

在交付前自己跑过这些,大幅减少返工:

- [ ] `image/` 能 `docker build` 出来,**不**报错,产出 `cyberarena/chal-<name>:<version>` tag
- [ ] `docker run --rm cyberarena/chal-<name>:<version>` 起来后,**5 秒内**能用 `nc localhost <port>` 或 `python3 -c "socket()..."` 跟服务通话
- [ ] `docker exec <container> id arena_agent` 返回 uid > 0
- [ ] `docker exec <container> pgrep <服务进程名>` 返回非空
- [ ] **defense-capable 才查**:`docker exec <container> /arena/restart.sh` 退出码 0,再 pgrep 服务还能匹到
- [ ] flag 投放手测:`docker exec <container> ` 跑你 plant 的 shell 命令,然后跑 retrieve 命令,**取回来的串和投放的一致**
- [ ] `challenges/<name>/` 整个目录里没有任何**绝对路径 hardcode**(包括 user home / /tmp 之类的全局 path,除非是 image 内的固定位置)
- [ ] image **不需要** internet 才能起来(no `apt-get update` / `pip install` 在 entrypoint 里)

## 5. 完整目录树参考

最终交付应该长这样(以一个假设的 `myctf` 为例):

```
challenges/myctf/
├── __init__.py            (~3 行)
├── challenge.py           (~50 行)
├── functionality_test.py  (~50-80 行)
├── flag_handler.py        (~40-80 行)
├── restart_handler.py     (~60 行,可选)
├── prompts.py             (~50-150 行)
├── build.py               (~40 行)
└── image/
    ├── Dockerfile         (~50 行)
    ├── entrypoint.sh      (~20 行)
    ├── restart.sh         (~20 行,可选)
    ├── checker.sh         (~20 行,可选)
    └── server.py(或 ELF / Pascal source / etc.)
```

## 6. 常见坑

| 坑 | 怎么躲 |
|---|---|
| 忘了创建 `arena_agent` 用户 | 框架在 install agent 时会探测,有 fallback 到 root,但**警告 log**,会被 reviewer 卡 |
| 没禁 `PermitRootLogin` | 同上 — 安全 review 卡 |
| 服务用 `localhost` 而不是 `0.0.0.0` 绑定 | 容器内 health probe 走 `127.0.0.1` 没问题,但 cross-team attack 走 `0.0.0.0` 会失败 |
| entrypoint 里跑 `apt-get install` / 远程下载 | 网络抖一下全场崩;所有 install 必须在 Dockerfile build 阶段做完 |
| flag 直接打进 image 里 | flag **每轮 rotate**,build 时根本不知道值。plant 是 runtime 行为,不是 build-time |
| 服务起来太慢(几秒以上) | functionality_test 在 deploy 后立刻跑,慢启动 = 假阴性。要么 entrypoint 阻塞到 ready,要么 functionality_test 自己 retry 几次 |
| Dockerfile 不锁 `--platform=linux/amd64` | arm Mac 上 build 出 arm 镜像,EC2 amd64 上 pull 失败 |

## 7. 出问题怎么调

按这个顺序排查:

1. `docker logs <container>` — entrypoint / 服务起没起
2. `docker exec <container> bash` 进去看 — 文件在不在,权限对不对
3. `docker exec <container> ss -tlnp` — 监听端口对不对
4. 跑 `python -c "from challenges.<your_name> import YourChallenge; c = YourChallenge(); print(c.functionality_test.run(...))"` — 在 Python 里直接调你的 functionality_test,看返回的 CheckResult 树
5. 实在不行,找我们,**带上**:
   - 你的整个 `challenges/<name>/` 目录
   - `docker logs` 输出
   - 你跑过的 self-check 里哪一步过不了

## 8. 提交流程

1. fork repo / 在 feature branch 上工作
2. 你的 `challenges/<name>/` 目录是**唯一**新增的代码 — 不要改框架其他文件
3. PR description 里附上:
   - challenge 来源(github / CTF 比赛 / 自研)
   - flag 投放方式
   - defense 是否启用
   - self-check checklist 哪几条过了
4. 我们 review + 在 `challenges/registry.py` 加 4 行 + 合并

---

**附:核心接口的源码**

如果你想看精确签名:[`challenges/interface/challenge.py`](../challenges/interface/challenge.py) + [`challenges/interface/probe.py`](../challenges/interface/probe.py)。这两个文件本身没有任何外部依赖,可以独立读。
