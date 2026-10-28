# Changelog

<!-- version list -->

## [Unreleased]

- Notify open pages of new releases through SSE without polling version metadata.
- Read cached version information without waiting for PyPI; keep the last known release when offline.
- Prompt open pages to refresh after a service upgrade while preserving comment drafts.

## v0.2.0 (2026-10-28)

### Bug Fixes

- Preserve legacy review anchors with fixed baseline
  ([#2](https://github.com/yiminspace/ablation/pull/2),
  [`3b76cd0`](https://github.com/yiminspace/ablation/commit/3b76cd09a53277fb384136ac88bc51e38b20326f))

- Resolve exact external logs for replay evidence
  ([#1](https://github.com/yiminspace/ablation/pull/1),
  [`149fccf`](https://github.com/yiminspace/ablation/commit/149fccfa5d4444ee16771bd2c64f8134e12bc2be))

- 保留旧版评论并校验导入分组和长 diff ([#3](https://github.com/yiminspace/ablation/pull/3),
  [`dcdf8c9`](https://github.com/yiminspace/ablation/commit/dcdf8c969f96186e17ce96a617f6bb42927a8dea))

- 等待外部日志入库后自动关联测试记录 ([#5](https://github.com/yiminspace/ablation/pull/5),
  [`d76308e`](https://github.com/yiminspace/ablation/commit/d76308e431d61a8e8dba81fdbaa2a12466856e80))

### Documentation

- Require replay log association before handoff
  ([#8](https://github.com/yiminspace/ablation/pull/8),
  [`1f839ba`](https://github.com/yiminspace/ablation/commit/1f839ba70a61fbd190a3612778897fdcdc80ede2))

- 对齐 ablation 中英文 skill 指引 ([#6](https://github.com/yiminspace/ablation/pull/6),
  [`a771914`](https://github.com/yiminspace/ablation/commit/a771914a49c1ec3cc62b03e73df2033cf0b1e8ba))

- 补齐中文版 skill 元数据和对应结构 ([#7](https://github.com/yiminspace/ablation/pull/7),
  [`31bf4d9`](https://github.com/yiminspace/ablation/commit/31bf4d9ed9cc7bd5859cf2df9e3afe29a007182f))

### Features

- Add complete replay history and detail drawers
  ([#9](https://github.com/yiminspace/ablation/pull/9),
  [`c6b849e`](https://github.com/yiminspace/ablation/commit/c6b849e5ab28bc62c0836d4dcedeb4ee00d1535a))

- Simplify original request reviews and source links
  ([#2](https://github.com/yiminspace/ablation/pull/2),
  [`3b76cd0`](https://github.com/yiminspace/ablation/commit/3b76cd09a53277fb384136ac88bc51e38b20326f))

- Simplify request review and link rationale to source
  ([#2](https://github.com/yiminspace/ablation/pull/2),
  [`3b76cd0`](https://github.com/yiminspace/ablation/commit/3b76cd09a53277fb384136ac88bc51e38b20326f))

- 在实验标题下实时展示测试日志 ([#4](https://github.com/yiminspace/ablation/pull/4),
  [`8905d67`](https://github.com/yiminspace/ablation/commit/8905d67e8ace99f023985c15605e3ea30e3e1b31))

- 对齐 Quarry 的自动发布与版本更新流程 ([#10](https://github.com/yiminspace/ablation/pull/10),
  [`52949cd`](https://github.com/yiminspace/ablation/commit/52949cd16c67051e1adb57a20066eaed6b5042a6))

- 按需求分组审阅提示词改动并保留原文 diff ([#3](https://github.com/yiminspace/ablation/pull/3),
  [`dcdf8c9`](https://github.com/yiminspace/ablation/commit/dcdf8c969f96186e17ce96a617f6bb42927a8dea))


## Unreleased

- Align releases and update discovery with Quarry: automatically version main commits, publish validated tagged packages to PyPI, and show periodic GUI update notifications with explicit upgrade commands and release notes.

- Group related prompt edits by requirement, with rationale, expected effects and GitHub source links; keep original line diffs and comment anchors available in both views.

- Show only candidate prompt changes in the default diff, with decoded text, line and word differences, collapsed context, without duplicate summaries or replay status; show modification rationale directly below the diff. Keep input history and scenario changes in the full request evidence.

- Add optional platform-neutral replay tracing headers, response ID/link extraction, and independently authenticated log lookups.
- Save external evidence with each run and display log links in the GUI without treating association failure as model failure.


## 0.1.0 — development preview

Initial open-source preview, distributed through GitHub Releases. This preview did not publish to PyPI or enable registry updates.

- Import request files, JSON logs, and HTTP captures into portable experiments, with configurable source endpoints and field mappings.
- Preserve source evidence, normalization notes, candidates, synthetic scenarios, model outputs, and assertion results.
- Review prompt, input, request, response, and check differences in a local GitHub-style interface.
- Focus the initial view on prompt changes and rationale, with detailed evidence and comparison settings available on demand.
- Submit anchored comments, copy a portable handoff, or dispatch a configured local agent.
- Keep explanations in the current review and retain candidate history for requested changes.
- Preserve historical failure counts and let follow-up questions reference previously reviewed responses after another replay.
- Reject credential-bearing request patches and source URL queries, and validate imported experiment bundles before saving.
- Record oversized replay results as execution errors and terminate remaining Agent child processes on cancellation.
- Provide cached update discovery, explicit package-manager upgrades, and offline release notes.

Replays call a configured model endpoint. Returned tool calls are recorded but never executed. Agent execution is tested with local stubs; no real-model behavior or desktop-chat wakeup is claimed.
