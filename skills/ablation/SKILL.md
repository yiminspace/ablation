---
name: ablation
description: Optimize or compare prompts using fixed request evidence, replay experiments, and a local diff-and-comment GUI. Use when asked to test prompt changes, inspect a replay, or respond to an ablation review.
---

# Ablation

Use the installed `ablation` CLI. Discover current flags with `ablation --help` and the relevant subcommand help. The CLI owns experiments, replays, comparisons, and reviews; the agent explains the problem, proposes changes, and interprets evidence. For Chinese guidance, read [中文说明](references/zh.md).

## Choose the evidence

Reuse the experiment, log link, capture reference, or file already established in the conversation. Do not ask the user to repeat a source tool name. An experiment already imported has a fixed baseline; continue it rather than fetching a fresh baseline. If several sources remain plausible, resolve that ambiguity before running.

For a conversation link, use an available conversation reader to locate the underlying request artifact or log reference. A conversation transcript or summary is context, not the original model request: select the relevant model call and verify its input, instructions, tools, parameters, and replay target before importing. If the raw evidence is unavailable, state what is missing rather than treating the conversation as a faithful replay source.

Accept request JSON, exported logs, and HTTP captures. Choose `request`, `log`, or `capture` explicitly; use a configured profile's JSON Pointers when the source shape needs mapping. Pointers are relative to the original source root; missing fields are errors. Inspect `ablation config profile --help` before creating a mapping.

Remote retrieval needs an explicit source base URL and either a JSON URL on the same origin or a reference resolved by the configured path template. A webpage sharing link may require exporting JSON with an existing tool first. Keep source credentials separate from inference credentials; configuration stores environment-variable names, never secret values. Do not invent endpoint addresses or assume source and replay environments are identical.

Inspect normalization notes and the source-to-request diff. A reconstructed log is not the original wire request. Preserve the complete tool schema and all request fields except the declared edits.

## Design and run an experiment

State the intended behavior and the smallest plausible prompt change. Save a candidate with rationale. Keep candidate prompt changes, scenario input changes, and evaluation checks distinct. Synthetic scenarios need a plain-language description of exactly which facts were changed and why; never present them as observed production events.

Write the candidate rationale as two short sentences or paragraphs: first the problem and intended improvement; then the conceptual code location and impact. Link a verified GitHub source location using `[human-readable location](https://github.com/OWNER/REPO/blob/COMMIT/path#Lx-Ly)` rather than listing filenames in prose. Verify the repository, revision, and lines; distinguish client-provided tool fields from backend definitions or transformations before claiming the scope. Describe parameter or client effects only when supported; mark unknown origins or locations as pending confirmation. Pass the text through `--rationale`, or use `--rationale-file` for multiline UTF-8 text when supported by the installed CLI. The GUI renders source links within the rationale below the diff; no separate Check view is needed.

Run comparable baseline and candidate requests under the same scenario and profile. Use a bounded number of repeats appropriate to the question. Assertions are explicit checks of the actual output; absent checks mean unscored, and network failures are not behavioral failures. Avoid writing a rule that mentions the particular test answer or downstream implementation label only to pass one example.

The GUI is the human interface for inspecting diffs and writing review comments. The CLI is the LLM interface for all experiment operations, evidence retrieval, result analysis, statistics, assertions, diffs, and review replies. Use the CLI to launch the GUI and provide its URL for the user; verify launch through CLI status rather than browser inspection. Read human review comments and reply through the CLI. If required information is missing from the CLI, report that interface gap instead of switching to GUI automation or reading the webpage. Browser inspection belongs only to a separate, explicit request to test the GUI itself.

Explain observed behavior and uncertainty; repeating one example does not demonstrate general reliability. Model tool calls are recorded evidence, not executed actions or proof of a real-world outcome.

## Review loop

Review handoffs contain experiment and review IDs, the reviewed candidate/scenario, anchored snippets, user comments, and intent. Reuse these IDs and the explicit workspace so replies return to the existing GUI.

- `comment` intent means explain or investigate. Add a threaded reply; do not create a candidate or replay merely to answer why a change was made. Ask within the review if the intended action is unclear.
- `changes` intent permits the requested experiment edits and bounded reruns. Preserve the old candidate and create the next one. Report which artifacts confirm the result.
- For a copied handoff, use its documented reply command to persist the answer. When invoked by the automatic runner, return the answer as the final message; the runner saves it.

Opening the GUI or saving a review does not start an agent. Automatic dispatch uses a configured local agent runner; it does not promise to wake an existing desktop chat. Otherwise copy the review into the current conversation.

Candidates remain experiment artifacts. Apply a patch to a business repository only when the user asks. Tool upgrades preserve experiment data and do not install or synchronize skills.
