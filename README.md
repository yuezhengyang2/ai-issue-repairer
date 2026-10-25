# Issue Repair Agent

面向小型 Python 仓库的代码修复 Agent，使用 OpenAI Responses API 的 function calling。
输入 Bug 描述，模型按需调用文件检索、读取、精确替换和 Docker 测试工具，输出补丁与验证报告。

## 快速开始

需要 Python 3.11+、OpenAI API Key，以及运行 Linux 容器的 Docker。
开发机本身不需要安装 pytest；它安装在测试镜像里。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
docker build -t issue-agent-tests:local .

# 在本机设置，不要把真实密钥提交到 Git 或发到聊天里。
$env:OPENAI_API_KEY = "你的 API Key"
$env:OPENAI_MODEL = "gpt-4.1-mini"

.\.venv\Scripts\python issue_agent.py examples/buggy_repo --issue "parse_date 遇到空字符串应返回 None，其他日期解析保持正常"
```

默认模型是一个支持工具调用的可配置起点，不代表当前最便宜或最新模型；请使用自己账户可访问的模型。
本项目不会自动读取 `.env`。API 调用和第一次构建 Docker 镜像均需要网络。
本轮修改通过 24 项本地单元测试，并于 2026-10-25 完成真实 Docker 验证：示例修复前 1 failed / 1 passed，预设修复后 2 passed，空测试集返回退出码 5，45 秒超时后容器已清理。本轮未调用真实 AI API。

## 常用参数

```powershell
python issue_agent.py examples/buggy_repo --issue "空日期返回 None" --max-edits 3 --max-steps 8 --token-budget 25000
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--model` | `OPENAI_MODEL` 或 `gpt-4.1-mini` | 选择账户可用模型 |
| `--max-edits` | `3` | 最多成功替换次数；失败替换不计数 |
| `--max-steps` | `8` | 最多 API 请求轮数 |
| `--token-budget` | `25000` | 累计 Token 预算 |
| `--max-output-tokens` | `1200` | 每轮输出上限 |
| `--image` | `issue-agent-tests:local` | 包含 Python、pytest 和项目依赖的 Linux 镜像 |

达到修改上限后仍可读取文件和运行测试。每次成功替换都会清除之前的测试通过状态，必须重新测试。
空问题描述、非正数限制和不可用仓库会被拒绝。退出码 `0` 表示产生修改且修改后测试通过，`1` 表示未验证修复，`2` 表示启动参数或准备环境有误。

## Function calling 的实现

核心代码在 `issue_agent.py`：

1. `TOOLS` 声明严格 JSON Schema：所有字段必填、拒绝额外参数。
2. `run_agent()` 通过 `client.responses.create()` 发送工具定义和消息。
3. 保留 `response.output`，逐个处理类型为 `function_call` 的输出。
4. `Workspace.execute()` 再次校验工具名、参数类型和路径，然后执行。
5. 把执行结果作为 `function_call_output` 追加到上下文，`call_id` 对应原调用。
6. 模型继续调用工具，或输出最终说明；到达预算、轮数或异常时停止。

工具执行错误会返回给模型自行纠正；网络/API 错误不自动重试。截断的模型响应不会执行工具。
不向模型提供任意 shell 工具。测试命令由程序固定。

## Token 控制

- 默认最多 8 轮 API 请求，每轮输出上限 1,200 Token。
- 默认累计预算 25,000 Token，通过 `--token-budget` 调整。
- 每次请求前用 UTF-8 字节数加预留量保守估算输入，并预留输出额度；请求后累计 API 返回的实际 usage。
- 该预算是应用层的提前停止机制，**不是精确计费硬上限**；估算可能提前停止，异常请求也可能产生无法统计的费用。
- 按需读取，每次最多 80 行，搜索最多 15 条，工具回传最多约 5,000 字符。
- 完整历史会重新提交并计入后续输入；不使用 `previous_response_id` 规避统计，也不承诺历史免费。
- SDK 设置 `max_retries=0`，默认不使用多 Agent。

## 输出与边界

每次任务生成 `runs/<任务ID>/`：

- `workspace/`：仓库副本，修改只发生在这里。
- `fix.patch`：供人工审查的 unified diff。
- `events.jsonl`：请求轮数 `step`、工具调用标识 `call_id`、参数与结果，可能包含仓库源码。
- `report.json`：任务状态、实际 Token 用量、测试是否通过、修改次数与总耗时。

报告中的 `test_runs` 按顺序保存每次测试的 `edit_count`、`duration_seconds`，以及退出码/输出或错误。
`edit_count=0` 的记录可用于查看修改前的基线；它只在实际调用测试工具时产生，不能把没有基线记录理解为已复现 Bug。
`status=finished` 表示模型结束回答，是否通过验证仍以 `verified` 为准。

`verified=true` 仅表示修改后当前 pytest 测试通过，不证明修复在所有场景正确。
示例仓库故意包含一个失败测试。框架单元测试请使用下面的命令，不要把示例失败测试当作框架回归。

MVP 支持最多 200 个文本文件、总计 1 MB、单文件 100 KB；跳过隐藏文件、链接及常见生成目录。
快照按字节保留 UTF-8 文件（包括 LF/CRLF 换行），拒绝非 UTF-8 文件和没有受支持文件的仓库。
替换时保留文件原有的统一 CRLF 换行格式，补丁也保留对应行尾，避免引入无关的整文件改动。
只允许修改非测试 Python 文件，不允许新增文件或自动安装项目依赖。
复杂项目需要预先构建包含依赖的镜像，并通过 `--image` 指定。
文件名过滤不是完整的密钥扫描器，运行前应确认目标仓库中哪些内容可以发送至 API。

测试容器禁用网络，挂载仓库为只读，限制内存、CPU、进程数和执行时间，不挂载 API Key。
程序固定工作目录和入口，执行 `python -m pytest -q -p no:cacheprovider`，不使用自定义镜像的默认入口命令。
测试超时后会尝试强制清理容器；没有收集到测试（pytest 退出码 `5`）不会视为通过。
Docker 隔离用于个人开发原型，不构成面向任意恶意仓库的生产安全保证。
断点恢复、GitHub 接入、独立评估集和 Web 界面尚未实现。

## 不花 API 额度的本地验证

```powershell
python -m unittest -v test_issue_agent
python issue_agent.py --help
```

当前共 24 项标准库 unittest 测试，模拟 OpenAI 与 Docker 返回值，覆盖调用结果关联、预算、异常、路径限制、大小写测试文件保护、换行保留、快照边界、容器清理及修复闭环。
如果开发环境另外安装了 pytest，也可执行 `python -m pytest -q`；根目录的 `pytest.ini` 默认只收集框架测试，排除故意失败的示例及 `runs/` 中的任务副本。
实现依据已有 API 知识并对照本机已安装 SDK 的参数签名，未下载在线文档。

启动 Docker 并准备好测试镜像后，可以运行真实容器验证（不需要 API Key，也不会调用 AI API）：

```powershell
python verify_docker.py
# 使用自己的依赖镜像时：
python verify_docker.py --image issue-agent-tests:local
```

该脚本在 `runs/docker-check-<ID>/` 中创建示例副本，依次验证原始 Bug 复现、预设修复后的测试通过、空测试集拒绝、45 秒超时以及容器清理，约需一分钟。
它还检查示例原文件和副本中的测试文件未被修改，并保存 `fix.patch` 与 `report.json`。全部检查通过时退出码为 `0`，否则为 `1`。
这里的修复由脚本预设，用于验证本地执行链路，不代表模型自主修复效果或真实 API 验证。

## 常见问题

- Docker 连接失败：启动 Docker Desktop 并切换到 Linux 容器，然后执行 `docker image inspect issue-agent-tests:local`；镜像不存在时先执行快速开始中的构建命令。
- `ModuleNotFoundError: openai`：在实际运行项目的解释器环境中执行 `python -m pip install -r requirements.txt`。
- 测试中缺少项目依赖：预先构建包含依赖的镜像，再通过 `--image` 指定。任务运行期间不会联网安装依赖。
- `status=token_budget`：预算不足以覆盖下一轮上下文和预留输出；可缩小仓库/问题范围，或提高预算后重新运行。
- `status=incomplete`：模型响应没有完成，不会执行该轮工具调用；检查模型输出限制后重新运行。
