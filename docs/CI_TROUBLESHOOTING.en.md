# Historical build-check notes

[简体中文](CI_TROUBLESHOOTING.md) | English · [Back to README](../README.en.md)

## Current approach: local builds

GitHub Actions is disabled and the three hosted build workflows have been removed. Development, testing, packaging and installation checks run on your own computer. No paid GitHub allowance, cloud server or paid build service is required. See [local building and distribution](LOCAL_BUILD.en.md).

Use `scripts/build-windows-wheel.ps1` on Windows and `scripts/build-macos.sh` on Apple Silicon. Users installing a prebuilt native wheel do not need Go, Node.js or Wails. The source research CLI can be installed directly through pip.

## Why an old commit shows four failures

On 2026-09-24, Windows desktop wheel, macOS, and Python package on Windows/macOS did not start for commit `89e55d1`. GitHub's annotation identified failed account payments or a spending limit, with no actual compilation/test logs. The specific billing condition was not established. This run belongs to the former repository (run ID `35971302887`); it will no longer be available after that repository is deleted.

The project no longer depends on that build pipeline. Using or distributing the software does not require purchasing Actions allowance. Historical failures may remain in commit history. Disabling the service does not turn them into passing results or clear past usage or existing bills.

## Validating the current version

Local scripts stop when tests, engine freezing, desktop compilation or native-wheel installation checks fail. Validate Windows x64 and Apple Silicon on the respective systems. Source-package and native-desktop checks cover different layers; one platform's result cannot substitute for another's.

For `unsupported wheel`, check the filename, system/Python architecture and macOS 15+ requirement. Investigate dependency, compilation and test errors using local terminal logs. If no installer is available, build locally or obtain a maintainer-supplied package instead of depending on temporary Actions artifacts.
