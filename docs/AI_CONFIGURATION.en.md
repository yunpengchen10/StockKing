# AI configuration manual

[简体中文](AI_CONFIGURATION.md) | English · [Back to README](../README.en.md)

Applies to Stock King 2.6. Menu labels are primarily Chinese. Quotes, watchlists, local picks and quantitative research do not require an AI API key. AI organizes evidence, explains risks and compares opinions; it does not replace local entry checks.

Prefer the free local workflow: leave AI disabled, use Ollama as described below, or manually import existing reports. Cloud configuration is optional for users who already want their own integration; installation and routine research do not require purchasing a cloud AI plan.

## 1. Add and save a platform

1. Open **工具 → AI 平台配置 → 添加AI配置** (Tools → AI platform configuration → Add).
2. Enter a configuration name, Base URL, API key and model name. The configuration name is a local label; the model name must be the exact API Model ID.
3. Click **测试并刷新模型列表** (Test and refresh models), then select a model your account can access. If the provider does not expose a model list, enter the exact ID manually. A failed list request alone does not establish that generation is unavailable.
4. Click **确定** (Confirm) in the drawer, then **保存配置** (Save configuration) on the list page. Confirming the drawer alone does not persist the configuration.
5. Return to **AI 研究**, select the platform, model and symbol, and run one manual research request to verify generation. Evidence from local picks may be used as context; launch any model interpretation separately in **AI 研究**.

The legacy entry point is **设置 → AI设置 → AI诊股 → 前往管理**. Its switch does not replace platform configuration or mean that scheduled picks automatically call a model.

## 2. Fields and examples

| Field | Meaning and guidance |
| --- | --- |
| 配置名称 — Configuration name | A local label such as `DeepSeek-Research` or `Local Ollama` to distinguish platforms. |
| 接口地址 — Base URL | The API base, not a chat website. Do not append `/chat/completions`; retain any version prefix required by the provider. |
| 令牌 — API key | Enter only on your machine. Being logged into a chat account is not an API credential. |
| 模型名称 — Model ID | Select a listed model or enter its actual provider/local ID. Do not copy a retired default model name. |
| Temperature | Sampling parameter; a lower value is a reasonable starting point for evidence summaries. Some models restrict or ignore it; follow provider guidance. |
| MaxTokens | Maximum output amount within the model/endpoint limits. Verify manually when model-list metadata is incomplete. |
| Timeout | Measured in seconds, with a UI minimum of 60. Longer reports or reasoning models may need more time. |
| 深度思考 — Deep thinking | Enable only when supported by the model and endpoint. OpenAI-compatible services do not necessarily share reasoning extensions. |
| HTTP代理 — HTTP proxy | Enable and enter an address if your network requires it. Model listing uses the general HTTP client; verify the per-configuration proxy with an actual generation request. |

### Cloud OpenAI-compatible services

For DeepSeek, a configuration might use `DeepSeek-Research`, Base URL `https://api.deepseek.com`, a key created in your corresponding API account, and a model selected from the account's current list. Refer to the [official DeepSeek documentation](https://api-docs.deepseek.com/) for models, parameters and billing.

For other services, use the documented base (some require `/v1`) and exact model ID. The application uses Chat Completions; endpoints that support only other protocols need an adapter. A generic AI compatibility claim is insufficient. A provider appearing in the configuration page also does not establish that every deployment type has been tested.

### Local Ollama

| Field | Example |
| --- | --- |
| Configuration name | `Local Ollama` |
| Base URL | `http://localhost:11434/v1` |
| API key | `ollama` (ignored by the default local server; the application form requires a nonempty value) |
| Model ID | The exact installed model tag shown by `ollama list` |
| Deep thinking | Disable for the first verification, then adjust for model compatibility |

Install and start Ollama, download a model suited to your machine's memory, and confirm it exists with `ollama list`. Typing a model name does not download it. If an authentication proxy protects the service, use that proxy's actual authentication; the placeholder is not a credential. See [Ollama's OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility). These are configuration examples, not a claim that every local model has been tested with Stock King.

## 3. Using AI in the pages

**AI 研究 — AI research:** Select a symbol and template, review retrieved evidence timestamps and gaps, then choose a platform/model and run. When saving or comparing reports, inspect analysis time, expiration, provenance and verification flags. Model-generated text cannot turn missing quotes into verified facts.

**精选 → 当日机会 — Daily opportunities:** The local scan shows conservative/balanced/aggressive conditions and candidate evidence. To have a model summarize evidence, risks or disagreements, select the symbol and run a separate request in **AI 研究**. Research output does not change local ranking or bypass entry checks.

**External reports:** AI research can export a research package and manually import an external AI report in Markdown, TXT or JSON. Enter source and time information as requested, then inspect parsing and verification flags. This route does not require saving a remote model key in the application. You still decide what to share when submitting an exported package to another service.

## 4. Data and costs

- Configurations are stored in local application data; do not treat this as an operating-system credential vault. Do not commit configuration databases, `.env` files or real keys to GitHub, or share screenshots containing keys.
- Remote AI requests send selected evidence, prompts and request content to the provider. Local storage does not imply offline AI processing. The local Ollama example points to a service on your machine.
- Adding a platform does not make routine scheduled picks, reviews or local learning use remote AI. Review separately enabled bots, research assistants or other AI scheduled tasks individually.
- Providers manage API costs and quotas. Start with one small manual research request and inspect provider usage before increasing output length or using a reasoning model.

## 5. Common errors

| Symptom | What to check |
| --- | --- |
| Model-list test passes but generation fails | The list test only calls `/models`. Check generation permissions, exact model ID, balance, output limits, reasoning parameters and the actual generation error. |
| Empty model list or list endpoint returns 404 | Check base/version prefixes. The service may not implement model enumeration. Enter a documented Model ID manually, then test generation. |
| 401 / 403 | Check key ownership, validity and model access. Never paste a key into an issue report. |
| 404 / model not found | Check for a chat website URL, duplicated path or incorrect Model ID. Providers may reuse status codes for different errors; read the response. |
| 429 / quota / balance | Distinguish rate limiting from quota/balance errors using the message. Rapid retries do not resolve these conditions. |
| Timeout / network error | Check service availability, DNS/network and required proxies. For local services, check the port and model loading. Increase Timeout when reasoning legitimately needs longer. |
| unsupported parameter | Check compatibility; first disable deep thinking and adjust sampling/output parameters. Persistent incompatibility needs an adapter. |
| Configuration disappears after restart | Confirm that you clicked “保存配置” on the list page and are using the same application data directory/system user. |

For troubleshooting, share only errors stripped of keys and personal data, along with provider name, Model ID, software version and reproduction steps.
