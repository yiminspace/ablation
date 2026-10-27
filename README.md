# Ablation

A local prompt debugging workbench. Import a model request or captured log, change a prompt, replay comparable cases, and review the evidence with GitHub-style diffs and inline discussions.

The CLI and browser GUI share one experiment store. The optional Agent skill uses conversation context to find the source; users can simply ask **“Use ablation to investigate this request.”** Import request files, exported logs, or HTTP captures, and configure field mappings for your source format.

[中文说明](README.zh-CN.md) · [Interface contract](docs/implementation-contract.md) · [Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)

## Install from source

Supports macOS and Linux with Python 3.11 or later. The GUI is included; end users do not need Node.js. This development preview is distributed through [GitHub Releases](https://github.com/yiminspace/ablation/releases); it is not published on PyPI.

```sh
git clone https://github.com/yiminspace/ablation.git
cd ablation
pipx install .
ablation --version
ablation demo --open
```

Alternatively, `uv tool install .`, or download the wheel from GitHub Releases and run `uv tool install /path/to/ablation_workbench-0.1.0-py3-none-any.whl`. For development use `python -m venv .venv` followed by `.venv/bin/python -m pip install -e .`.

The demo uses synthetic inputs and makes no model requests. Its initial output panel is explicitly untested. It includes an over-specific candidate and a more general candidate to demonstrate comparisons without presenting fabricated performance claims.

## From a request to an experiment

Keep connection configuration and experiments in a workspace:

```sh
ablation --workspace ./experiments init
ablation --workspace ./experiments config profile model \
  --base-url https://model.example.invalid \
  --api-key-env MODEL_API_KEY

ablation --workspace ./experiments import request.json --format request
ablation --workspace ./experiments gui
```

The example addresses are placeholders. Replace them with your own endpoints and supply credentials through the named environment variables. Configuration stores variable names, not secret values. Source retrieval and model inference use separate endpoint and credential settings.

Choose an explicit import format:

| Format | Input |
| --- | --- |
| `request` (default) | A Responses API or Chat Completions request object. Use `--response response.json` to attach a historical response. |
| `capture` | A JSON envelope containing `request` and optionally `response`; an HTTP envelope can place each JSON body in its `body` field. |
| `log` | A JSON log with request fields in `params`, optionally overridden by top-level `model`, `input`, `messages`, and `tools`; `response` is optional. Use mapping settings for other shapes. |

You can upload JSON through the GUI or pass a file or `-` for stdin to `ablation import`. Original evidence is retained separately from the replay request. Normalization notes and the source diff disclose reconstruction; missing mapped fields are errors rather than silently invented values.

For a log shaped as `{"payload":{"request":{...},"response":{...}}}`, configure JSON Pointers to those objects. The same mapping applies to a local export or a remote JSON API:

```sh
ablation --workspace ./experiments config profile logs \
  --source-base-url https://logs.example.invalid \
  --source-path '/records/{ref}' \
  --source-request-pointer /payload/request \
  --source-response-pointer /payload/response \
  --token-env LOGS_TOKEN

ablation --workspace ./experiments import source-log.json --format log --profile logs
ablation --workspace ./experiments import record-123 --ref --format log --profile logs
```

Omit `--source-response-pointer` when your export has no response. To map individual request fields, use `--source-fields-json '{"model":"/metadata/model","input":"/payload/messages"}'`; each pointer starts at the original source root and explicitly overrides that top-level request field. Fields in the selected request object or `params` remain intact unless explicitly overridden; other source fields remain in the original evidence.

Remote imports require an explicit `--source-base-url`. Supply a full JSON URL on the same origin, or a reference resolved by the configured `--source-path` template. `--ref` forces a positional value to be treated as a reference instead of a local file. Source authentication uses either `--token-env` or the pair `--source-user-env` / `--source-password-env`. It never falls back to the inference API key. A webpage sharing link is not necessarily a JSON endpoint; export its data with an existing tool, then import that JSON. Replay can independently use the `model` profile.

## Change, replay, compare

Candidates are immutable JSON patches with a rationale. Keep the explanation concise: why the change helps, then where it belongs and what it affects. Add verified GitHub source links using Markdown; identify unconfirmed locations or origins explicitly. Scenarios independently describe changed inputs and their expected behavior. Every candidate is derived from the fixed source; prior model responses never become the next baseline automatically.

```json
[
  {
    "op": "replace",
    "path": "/instructions",
    "value": "Summarize the supplied evidence in three concise bullets. State when a requested fact is missing."
  }
]
```

Save that as `candidate.json`. Pass explanation text with `--rationale`, or use `--rationale-file rationale.md` to preserve multiline UTF-8 text and source links. Both store the same rationale displayed below the GUI diff. Then:

```sh
ablation --workspace ./experiments candidate EXPERIMENT_ID \
  --label 'Concise evidence summary' --patch candidate.json \
  --rationale 'Specify output structure and how to handle missing information.'

ablation --workspace ./experiments run EXPERIMENT_ID \
  --candidate baseline --candidate CANDIDATE_ID \
  --scenario original --repeats 3 --profile model

ablation --workspace ./experiments diff EXPERIMENT_ID \
  --candidate CANDIDATE_ID --artifact response
```

The GUI offers the same candidate/scenario creation and replay controls. Runs save the actual request, response, request hash, timing and assertion results. Compare prompt, scenario input, full request, response, checks, and source reconstruction. HTTP failures are separate from failed assertions. A run without checks is **unscored**, not passing. Repetition counts are not independent scenario counts or statistical reliability claims.

The initial view fixes the imported original request on the left and shows the modified request on the right. Both sides retain the original JSON structure and field order. Instructions, tool descriptions, and input-message changes appear together, with unchanged context collapsed. Older versions and candidate/scenario creation live in **运行记录**. The main view contains only actual changes and model outputs.

The summary counts historical runs for the selected versions and scenario, so a later passing replay does not hide earlier failures. The response diff shows the latest completed outputs; inspect individual runs for earlier evidence.

The sidebar starts collapsed and lists saved experiment sessions by title and full session ID, with search and replay counts. Each session retains all replay batches, scenarios, repeats, outcomes, and log links. The initial view prioritizes the diff, with the complete batch history on the later **运行记录** tab; **运行记录** lists all runs compactly. Clicking anywhere on a batch row opens a narrow, full-height right drawer with Messages, checks, and raw JSON views. A selector switches between the runs in that batch. Its timeline includes recorded inputs, tool calls/results, screenshot thumbnails that open at full size, and the new model output. Opening a run positions the timeline at its highlighted new output; a locate button returns to it. The timeline adapts Bifrost's Apache-2.0 components; the pinned source, modifications, and license ship with the web assets. External log links remain available for provenance. The GUI uses Voyage components with GitHub as the default theme. The top-right standard VoyageToolbar provides light/dark and theme selection. Its feedback and language placeholders remain disabled until those product capabilities are configured. All styles and the toolbar are bundled locally for offline use; `npm run build:ui` synchronizes the pinned assets and `npm run check:ui` verifies them.

Replay invokes a single model request. Returned tool calls are inspected as data and never executed. A model response does not prove that an external action occurred.

## Optional external log tracking

Replay profiles may configure independent observers through `config profile NAME --observers-json observers.json`. The file is an array, so a request passing through a proxy and a model gateway can retain both records. No tracking headers or external lookups are enabled by default.

```json
[
  {
    "name": "Gateway logs",
    "headers": {"X-Trace-Id": "{run_id}"},
    "record_id_header": "X-Request-Id",
    "url_template": "https://logs.example.invalid/records/{record_id}"
  },
  {
    "name": "Capture proxy",
    "record_id_pointer": "/capture/entry_id",
    "url_pointer": "/capture/view_url"
  }
]
```

These are synthetic examples; select header names, response fields, and URL routes documented by your service. `headers` templates accept `{experiment_id}`, `{batch_id}`, and `{run_id}`. They cannot override authentication or transport headers. Response extraction supports JSON Pointers (`record_id_pointer`, `url_pointer`) and case-insensitive HTTP headers (`record_id_header`, `url_header`). Use `request_id_pointer` or `request_id_header` for transport correlation IDs that are not external log IDs; they remain separate and can be used as `{request_id}` in lookup templates. `url_template` and optional `batch_url_template` also accept `{record_id}`, `{response_id}`, `{start_time}`, and `{end_time}` (Unix seconds bounding this run); substituted values are URL-encoded. Escape literal JSON braces in templates as `{{` and `}}`.

For Bifrost, inject a unique run metadata header and configure an exact lookup against that metadata. Extract the actual log ID and construct its `selected_log` detail URL; do not use the transport `x-request-id` as the log ID. A metadata-filter search URL is only a list entry point. A TapeProxy target can extract an entry reference from its documented response header or API. Platform names are labels, not built-in routing: another service uses the same mappings without changing the replay engine.

An optional `lookup` reads a separate JSON log API after inference. It requires `source_base_url`, a relative `source_path` template, `match_pointer`, and `match_value`. Optional `records_pointer` selects the record array; `record_id_pointer` and `url_pointer` extract the uniquely matched record. Lookup authentication uses its own `token_env` or `source_user_env` / `source_password_env`, never the inference API key. For example:

```json
{
  "name": "Separate log service",
  "headers": {"X-Trace-Id": "{run_id}"},
  "lookup": {
    "source_base_url": "https://logs.example.invalid",
    "source_path": "/api/logs?trace={run_id}",
    "token_env": "LOG_READ_TOKEN",
    "records_pointer": "/records",
    "match_pointer": "/trace_id",
    "match_value": "{run_id}",
    "record_id_pointer": "/id"
  },
  "url_template": "https://logs.example.invalid/records/{record_id}"
}
```

A configured lookup is authoritative even when response extraction returns an ID or URL. Each lookup request has a 5-second timeout, requires one exact match, and follows no redirects. For delayed ingestion, set lookup `attempts` (1–5, default 1) and `interval_seconds` (0–5, default 1); only an empty exact-match result is retried, while ambiguous matches and request failures stop immediately. Missing or failed association is recorded as `unavailable` without changing the model result or assertion score. `available` means a record ID or link was obtained from the configured response or matching API record; `linked` means only a search/view URL was constructed, not that a log exists or is accessible.

Runs retain local tracking IDs, the injected non-secret headers, selected response headers, an observer configuration snapshot, and `external_evidence` records with `platform`, `status`, and optional `request_id`, `record_id`, `url`, `batch_url`, or `error`. The GUI displays links in run history and details; CLI JSON and exported bundles retain the same evidence. Profiles without observers still retain the model response ID and standard request-ID headers when supplied. Historical runs remain unchanged and import without the new optional fields.

## Review and continue

Click a diff line's **+**, add a comment, then finish the review. Comments retain the reviewed version, scenario, location and before/after snippets.

Use **继续追问** to ask about an existing comment. The follow-up preserves its original snapshot even if a newer replay has changed the response. Unsaved comments on changed responses still require reconciliation before submission.

- **复制到对话** (Copy to conversation) saves the review and copies a portable handoff. Paste it into the current Agent conversation. The handoff includes commands for writing an answer back to the experiment.
- **交给 Agent** (Send to Agent) invokes the configured local Agent and records a job. Configure Codex once with `ablation agent configure --backend codex --cwd /path/to/project`. Other Agents can use a trusted argv command that receives the handoff on stdin: `--backend command --command-json '["executable","argument"]'`.

**追问解释** (Comment) requests explanation only. Its replies appear below the reviewed change and do not produce a new candidate or replay. **请求修改** (Request changes) authorizes a new candidate and the requested verification within the experiment. Applying changes to a business repository remains an explicit user task for the Agent; this tool does not guess which source file generated a request field.

```sh
ablation review text EXPERIMENT_ID REVIEW_ID
ablation review reply EXPERIMENT_ID REVIEW_ID --comment-index 0 \
  --text 'This change makes the output format explicit while keeping the same source evidence.'
ablation review dispatch EXPERIMENT_ID REVIEW_ID
```

CLI dispatch waits until completion; Ctrl-C requests cancellation. The GUI shows progress, errors and cancellation. A completed Agent process is not a passing replay: inspect the resulting experiment evidence. The runner manages its own CLI sessions; it does not claim to wake an existing desktop chat.

## Portable skill

Copy `skills/ablation` into your Agent's supported skill directory. The skill supplies context selection, experiment design and result interpretation; the executable supplies mechanics and also works without a skill. Skill installation and synchronization are not performed automatically.

## Persistence and updates

The default workspace is `~/.local/share/ablation`; override it with `--workspace` or `ABLATION_WORKSPACE`. Experiments and review replies persist on disk. `ablation export EXPERIMENT_ID --out experiment.json` and `ablation import-bundle experiment.json` move experiments between workspaces; imports get new IDs and retain provenance.

Software versions, experiment schema versions and candidate IDs are separate. Historical request/response records remain unchanged when software updates. Credentials remain in your environment. Exported evidence can contain prompt content and model responses; review the bundle before sharing it.

Update checks in registry-published builds are cached for 24 hours, skip editable installations, and can be disabled with `ABLATION_UPDATE_CHECK=0`. This GitHub preview disables registry checks and upgrades until PyPI ownership and publication are configured. For now, install a reviewed newer wheel from GitHub Releases to update. `ablation update-check` reports availability; an unavailable registry is not proof that the installed version is current. In registry-published builds, `ablation upgrade` shows the installation-aware plan and `ablation upgrade --yes` executes it when no jobs are active. Restart the local GUI after upgrading. The changelog is bundled for offline viewing.

CLI stdout is JSON except for human review handoffs, changelogs and help. Errors go to stderr. Exit codes: `0` completed command (including unscored replay), `2` invalid input/configuration, `3` replay assertions failed, `4` execution or transport failure, `130` cancelled. Inspect saved per-run evidence even when the command returns nonzero.

## Development checks

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel --no-deps . -w dist
npm ci
npx playwright install chromium
npm run test:browser
```

Tests use synthetic fixtures and local source, model, and Agent substitutes. They do not establish compatibility with a particular live data service, model account, or desktop chat integration. Browser verification and release review details belong in `docs/release-review.md`.
