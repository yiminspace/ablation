# Changelog

<!-- version list -->

## Unreleased

- Group related prompt edits by requirement, with rationale, expected effects and GitHub source links; keep original line diffs and comment anchors available in both views.

- Show only candidate prompt changes in the default diff, with decoded text, line and word differences, collapsed context, without duplicate summaries or replay status; show modification rationale directly below the diff. Keep input history and scenario changes in the full request evidence.

- Add optional platform-neutral replay tracing headers, response ID/link extraction, and independently authenticated log lookups.
- Save external evidence with each run and display log links in the GUI without treating association failure as model failure.


## 0.1.0 — development preview

Initial open-source preview, distributed through GitHub Releases. PyPI publication and registry updates remain disabled.

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
