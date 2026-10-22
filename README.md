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
当前离线环境仅验证了本地代码及模拟调用，未验证真实 API 和 Docker 执行。

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
- `events.jsonl`：工具调用与结果，可能包含仓库源码。
- `report.json`：任务状态、实际 Token 用量、测试是否通过及是否修改文件。

`verified=true` 仅表示修改后当前 pytest 测试通过，不证明修复在所有场景正确。
示例仓库故意包含一个失败测试。框架单元测试请使用下面的命令，不要把示例失败测试当作框架回归。

MVP 支持最多 200 个文本文件、总计 1 MB、单文件 100 KB；跳过隐藏文件、链接及常见生成目录。
只允许修改非测试 Python 文件，不允许新增文件或自动安装项目依赖。
复杂项目需要预先构建包含依赖的镜像，并通过 `--image` 指定。
文件名过滤不是完整的密钥扫描器，运行前应确认目标仓库中哪些内容可以发送至 API。

测试容器禁用网络，挂载仓库为只读，限制内存、CPU、进程数和执行时间，不挂载 API Key。
Docker 隔离用于个人开发原型，不构成面向任意恶意仓库的生产安全保证。
断点恢复、GitHub 接入、独立评估集和 Web 界面尚未实现。

## 不花 API 额度的本地验证

```powershell
python -m unittest -v test_issue_agent
python issue_agent.py --help
```

测试使用标准库 unittest，模拟 OpenAI 与 Docker 返回值，覆盖调用结果关联、预算、异常、路径限制、测试保护及修复闭环。
实现依据已有 API 知识并对照本机已安装 SDK 的参数签名，未下载在线文档。
