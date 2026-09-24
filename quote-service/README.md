# Stock King 行情工具

免费云端部署使用 `worker/` 中的 Cloudflare Worker，无需 OpenAI 或 DeepSeek API 密钥。它提供同名 MCP 工具，复用腾讯／新浪报价解析、时间和盘口校验逻辑。单次最多 20 只，仅接收公开股票代码，不绑定数据库或交易账户。

安装 Node.js 后，在 `quote-service/worker` 运行 `npx wrangler login` 和 `npx wrangler deploy`，选择 Workers Free。ChatGPT 自定义应用使用部署结果的 `https://<worker域名>/mcp`，身份验证选“无身份验证”。这是公开只读接口；免费额度和公共上游可用性均有限制。上线后必须实际调用 `get_stock_quotes` 检查 `source_time`，不能把部署成功当作取数成功。

开发检查：`node build.mjs`（Node.js 24）后运行 `node --test worker.test.mjs`。Windows 和 macOS 使用相同命令。Worker 使用 `redirect: manual` 并拒绝非成功状态，因为 Cloudflare 不支持 `redirect: error`。

程序也可直接读取 `GET /quotes?codes=600519,603386,002487` 或 `GET /quotes/600519,603386,002487`，返回带源时间的 JSON，禁止缓存。旧 ChatGPT 对话若返回 `This conversation does not support developer MCPs`，需在支持插件的新对话中连接并实测；网页搜索读取器不保证能访问此接口。

以下 Python 方式适用于本机客户端或已有隧道的用户：

复用 `daily-engine/src/services/public_market_quotes.py` 的腾讯／新浪双源，通过 MCP 提供 `get_stock_quotes` 和 `get_quote_capabilities`。只读公共行情，不读取持仓、账号、密钥文件，不下单、不调用 AI。

在仓库根目录安装并验证（macOS 把 `.venv/Scripts/python.exe` 换成 `.venv/bin/python`）：

```powershell
.venv/Scripts/python.exe -m pip install -r quote-service/requirements.txt
.venv/Scripts/python.exe quote-service/probe.py 600519 603386 002487
```

本地 MCP 客户端使用 stdio：命令为上述 Python 的绝对路径，参数为 `quote-service/server.py` 的绝对路径。客户端可查询任意 1–20 个六位沪深股票代码，示例代码不构成固定股票池。

HTTP 模式：

```powershell
.venv/Scripts/python.exe quote-service/server.py --transport http --port 8766
```

只监听 `127.0.0.1:8766/mcp`，不提供文件访问或其他软件接口。每分钟最多 60 次请求，最多 4 个并发请求，请求体最多 16 KiB。公网反向代理必须配置 `STOCK_KING_QUOTE_TOKEN`（至少 32 字符）和 `--public-host` 精确域名，客户端通过 Authorization Bearer 传递令牌，不把密钥写入 URL 或仓库。

ChatGPT 云任务不能直接访问本机 localhost。推荐通过 [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 转发本服务的 stdio，再在 ChatGPT 开发者模式中添加并实测工具。隧道需对应账户权限、运行密钥和常在线电脑；本地测试成功不代表云端已经连接。未完成云端验证前，不应把任务配置标为修复完成。

使用官方 runtime 时，将其放入 `artifacts/tunnel-client/`，在 `artifacts/quote-tunnel.json` 中设置 `tunnel_id`，将运行密钥单独保存在 `artifacts/quote-tunnel.key`。运行 `.venv/Scripts/python.exe quote-service/connect.py` 会连接本机 HTTP 服务并启动隧道；`--check` 只检查本地配置。密钥文件由客户端读取，不放进命令行、URL或Git。`http://127.0.0.1:8767/readyz` 用于检查客户端就绪状态，最终仍须在ChatGPT中实测报价。

使用 `source_time` 判断行情年龄，在最终决策时重新计算；`checked_at` 只是检查时间。超过 30 秒、盘后或缺失数据保留为参考。09:20竞价虚拟成交价、匹配量、未匹配量不在此接口支持范围内。`execution_verified=false`，盘口不保证成交。
