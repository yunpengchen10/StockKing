# Build troubleshooting

[简体中文](CI_TROUBLESHOOTING.md) | English · [Back to README](../README.en.md)

## All checks fail within seconds

Open **Summary → Annotations** for the specific Actions run. On 2026-09-24, the four checks for commit `89e55d1`—Windows desktop wheel, macOS, and Python package on Windows/macOS—did not start. GitHub's annotation identified recent failed account payments or a spending limit that needed increasing. No actual compilation/test step logs were produced.

This is an account billing restriction on GitHub-hosted runners, not evidence of a software compilation failure. Those checks also cannot be described as passing. See the [affected Python package run](https://github.com/yunpengchen10/stock-king/actions/runs/35971302887) (private repository access required). The annotation does not distinguish failed payment from a spending limit; the account owner must verify which applies.

1. In GitHub account **Settings → Billing & licensing** (called Billing & plans in some interfaces), inspect payment status, Actions usage and budgets.
2. The account owner decides how to handle payment, allowance or budget changes. Making the repository public is unnecessary; disabling checks is not a repair.
3. Once unblocked, select **Re-run failed jobs / Re-run all jobs** for the three workflows on the latest code, or use **Run workflow** on `main`. Python package includes two platform jobs, giving four checks in total.
4. Verify that steps actually execute, checks pass and artifacts exist before downloading a platform package. Investigate any subsequent source/dependency failure separately using that run's logs.

Private-repository runner usage and storage can be subject to account allowances and charges; see [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions). Repeated pushes or reruns will not resolve an outstanding account restriction.

## Build coverage in this repository

| Workflow | Target and output |
| --- | --- |
| Windows desktop wheel | Windows x64 native desktop wheel and installation verification. |
| macOS | Apple Silicon, macOS 15+ application and native wheel; no Intel Mac build. |
| Python package | Research package installation and tests on Windows and macOS. The generic research package is not the complete native desktop package. |

The workflow updates retain code builds and tests while reducing repeated usage:

- Code/build-configuration changes on `main` continue to trigger checks. Changes limited to `README*.md` or `docs/` skip these three packaging workflows. `workflow_dispatch` remains available for manual runs.
- A newer run for the same workflow and branch cancels the older run, avoiding duplicate builds.
- Newly uploaded artifacts are retained for **14 days**. This does not retroactively change existing artifacts or immediately remove an account billing restriction.
- A commit changing workflow configuration as well as documentation still triggers CI. Skipping documentation builds does not mean a code check passed.

See [GitHub workflow trigger documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow) for path filters. If these workflows later become required PR checks, account for the pending state that path-based skipping can cause.

## Installation failures versus build failures

For `unsupported wheel`, first check the filename, operating system, Python architecture and minimum macOS version; it does not establish a GitHub build failure. An `expired artifact` has passed its retention period and needs rebuilding or a previously downloaded copy. Actual dependency installation, compilation or test failures require the corresponding step logs. A successful old commit cannot substitute for validation of the current commit.
