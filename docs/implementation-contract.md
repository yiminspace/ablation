# Local implementation contract

The first release is a local, single-user prompt experiment workbench, distributed as an open-source GitHub development preview. No hosted service, automatic skill installation/synchronization, or business repository mutation.

Python 3.11+ package `ablation_workbench`; executable `ablation`; provisional distribution `ablation-workbench`. Standard-library core and packaged vanilla HTML/CSS/JS. Experiments are durable JSON under `<workspace>/experiments/<id>/experiment.json`. CLI and GUI share the engine. All documented JSON structures use `schema_version: 1`. A configured inference endpoint is called only on explicit run; tool calls returned by a model are evidence and are never executed.

## Shared Python interfaces

`engine.py` exports `Workbench(workspace: str | Path)`:

- `list_experiments() -> list[dict]`
- `get(experiment_id) -> dict`
- `import_source(source: dict, *, title: str = "") -> dict`: source = `{kind: "request"|"log"|"capture", data?: object, path?: str, url?: str, ref?: str, profile?: str, response?: object}`. Data/path is local import; remote source requires an explicit source profile. A URL addresses JSON on the profile's configured origin; a reference resolves through `source_path`. Never put credentials into saved source data.
- `add_candidate(id, *, label: str, patch: list, rationale: str, parent: str = "baseline") -> dict`: returns updated experiment. JSON Patch `add/replace/remove`, JSON Pointer; preserve full tools arrays and fixed baseline. Candidate `{id, label, parent, patch, rationale, created_at}`.
- `add_scenario(id, *, label: str, patch: list, description: str, origin: str = "synthetic", checks: list | None = None) -> dict`: returns updated experiment. Scenario `{id, label, origin, description, patch, checks}`. Immutable; changes create a new ID. Checks are explicit JSON assertions over actual response, not instructions.
- `run(id, *, candidates: list[str], scenarios: list[str], repeats: int = 1, profile: str = "", cancel_event=None) -> dict`: saves actual requests, responses, response errors, hashes, timings, per-assertion results. Return updated experiment. Enforce bounded repeats/calls; external errors are not assertion failures. Stop on cancellation between calls.
- `compare(id, *, base="baseline", candidate="baseline", scenario="original", artifact="request") -> dict`: `{base_label,candidate_label,artifact,before,after,rows:[{old_line,new_line,before,after,kind}],rationale,notes}`. Artifacts `prompt`, `input`, `request`, `response`, `checks`, `source`. Scenario input diff = original vs scenario; prompt/request = base vs candidate under same scenario; response selects latest matching completed runs and says untested when absent. Source = original source evidence vs normalized request, clearly labelled.
- `submit_review(id, review: dict) -> dict`: `{id, created_at, revision, base, candidate, scenario, intent:"comment"|"changes", comments:[{artifact,old_line,new_line,before,after,text}], summary,replies:[],parent_review?:{id,comment_index}}`. Stable client `id` makes retries idempotent; reviews bind immutable candidate/scenario and snapshot. A follow-up references one existing comment, preserves its location and snapshot, and supplies new question text. Return persisted review; reject conflicting reuse.
- `reply(id, review_id, *, text: str, comment_index: int | None = None, author="agent") -> dict`: writes durable threaded reply, returns review; no candidate or run created for explanation.
- `review_text(id, review_id) -> str`: full portable handoff with source/evidence paths, reviewed locations, comments, intent; explains CLI reply command and forbids changes/replay when comment intent.
- `export_bundle(id, path) -> Path`, `import_bundle(path) -> dict`: versioned JSON bundle, preserves history. Never overwrite existing experiment on import.

Experiment public shape: `{schema_version,id,title,created_at,revision,source:{kind,ref,fidelity,notes,evidence},baseline:{request,response,hash,api_kind},candidates:[...baseline included...],scenarios:[...original included...],runs:[],reviews:[]}`. `revision` monotonically increases on every mutation; it is not a candidate version. Runs include `{id,candidate,scenario,repeat,status,request,response,request_hash,checks,passed,created_at,elapsed_ms,software_version,recorded_revision,error?}`. No passed=true if no checks. Checks support `{kind:"json_equals",pointer,value}`, `{kind:"text_contains",value}`, `{kind:"tool_called",name}` and `{kind:"tool_argument_equals",name,pointer,value}`; the last parses structured tool arguments before checking the pointer. Empty checks mean unscored. HTTP responses support Responses API and Chat Completions, JSON and SSE.

Config in workspace/config.json: `{schema_version:1,profiles:{name:{base_url?,api_key_env?,source_base_url?,source_path?,source_request_pointer?,source_response_pointer?,source_fields?,token_env?,source_user_env?,source_password_env?}},agent:{...}}`. Credential fields contain environment-variable names, never secret values. Inference uses `base_url` and `api_key_env`; source retrieval requires `source_base_url` and uses either `token_env` or the pair `source_user_env` / `source_password_env`, never both. It does not inherit the inference endpoint or API key.

Source normalization is explicit:

- `source_request_pointer` selects a complete request object from the original source. Otherwise `request` uses the original object, `capture` uses `request` or its JSON `body`, and `log` copies all of `params` before overriding with any top-level `model`, `input`, `messages`, and `tools` fields.
- `source_fields` maps top-level request field names to JSON Pointers in the original source. These mappings explicitly override or add those request fields; preserve all unmapped fields, including unknown parameters and false/zero values. The CLI accepts this object through `--source-fields-json`.
- `source_response_pointer` selects the historical response from the original source. Otherwise `capture` uses `response` or its JSON `body`, `log` uses `response`, and `request` has no default response. Explicit `response` input remains supported.
- Every pointer starts at the original source root. Missing pointer targets are errors. Mapping or log reconstruction is labelled `reconstructed`; extracting a capture body is `recorded-json-body`, not a claim to preserve the original wire bytes. Do not add request defaults during normalization.
- `source_path` is a relative path, optionally containing `{ref}`. Remote import accepts a full JSON URL on the configured source origin or a reference resolved by this path template. Webpage sharing links are not a generic API contract; export JSON first when needed.
- CLI import supports existing local files, stdin via `-`, JSON URLs, and references with an explicit profile/path template. `--ref` forces a positional value to be interpreted as a reference. `--format` is `request`, `log`, or `capture`, defaulting to `request`; do not guess a source format from a vendor-specific URL or ID.

Scenario origin is `source`, `real`, `derived`, or `synthetic`; the built-in demo is entirely synthetic. A batch permits at most 30 requests and 10 repeats. The GUI summary counts all historical runs for the selected versions and scenario, including failures and unscored runs. A new review binds the current snapshot: an outdated response draft is rejected if a newer run changed its content, and the GUI retains the draft for reconciliation. A follow-up explicitly linked to an already saved review uses that historical snapshot. Dead run owners are recovered during reads; a response that exceeds the document limit is recorded as an execution error without a passing score.

`runner.py` exports `AgentRunner(workbench)` with `start(experiment_id, review_id)->dict`, `status(job_id)->dict`, `list_jobs(experiment_id=None)->list[dict]`, `cancel(job_id)->dict`. Agent configuration `{backend:"codex"|"command",command?:[str],cwd?:str,timeout_seconds?:int}`. Codex CLI noninteractive `exec --json` and resume explicit saved thread ID; no shell or hidden desktop-session assumptions. Configured custom command gets generated review prompt on stdin. Save jobs on disk; one active job per experiment; submitting same review returns existing job. Log/exit status do not imply expected changes or replay happened. Replies should appear in review after completion. Comment intent is read-only explanation; changes intent may invoke ablation CLI to add candidate/run. Do not run a live agent during implementation tests.

`updates.py`: `update_info(workspace, *, force=False)->dict`, `upgrade_plan()->dict`, `perform_upgrade(workspace)->dict`, `changelog()->str`. Update checks cached 24h, bounded timeout, disabled by ABLATION_UPDATE_CHECK=0 or editable install. GitHub previews keep registry checks/upgrades disabled until PyPI publication is configured; missing package is unavailable, never fabricate available. Upgrade through detected package manager, never shell; refuse while jobs/replays active, restart GUI explicitly. No skill synchronization. Installed changelog available offline.

## Local HTTP API

Loopback only, validate Host/Origin, require `X-Ablation-Client: local` on mutations. Errors `{error:{code,message}}` with non-2xx. No API reads arbitrary paths. Files imported through GUI are uploaded JSON content. Recognized credential fields and configured secret values are redacted. Source URLs reject credential query fields; patches that introduce credentials into the effective request are rejected. Prompt and response text still require review before sharing.

- GET `/api/experiments` -> `{experiments:[...]}`; GET `/api/experiments/<id>` -> experiment.
- POST `/api/import` source + title -> experiment; POST `/api/demo` -> synthetic example experiment.
- GET `/api/experiments/<id>/diff?base=...&candidate=...&scenario=...&artifact=...` -> compare.
- POST `/api/experiments/<id>/candidates` and `/scenarios` -> updated experiment.
- POST `/api/experiments/<id>/reviews` -> persisted review.
- GET `/api/experiments/<id>/reviews/<rid>/text` -> `{text}`.
- POST `/api/experiments/<id>/reviews/<rid>/replies` body text/comment_index -> review.
- POST `/api/experiments/<id>/reviews/<rid>/dispatch` -> agent job.
- POST `/api/experiments/<id>/run` body candidates/scenarios/repeats/profile -> background job; GET `/api/jobs` -> `{jobs:[...]}`; POST `/api/jobs/<id>/cancel`.
- GET `/api/experiments/<id>/export` -> bundle JSON download.
- POST `/api/import-bundle` -> imported experiment with a new ID.
- GET `/api/config` -> sanitized config/profile names and agent backend presence.
- GET `/api/update`, GET `/api/changelog`, POST `/api/upgrade`.

GUI locations are `/?experiment=<id>`. Poll selected experiment/jobs every few seconds, preserve drafts/focus/selection and render only when changed. Pending comments remain drafts until submitted; draft anchors never follow a newly selected revision. Copy submits/persists then copies review_text; automatic dispatch submits/persists then starts configured Agent. Each thread allows reply; show agent result underneath, do not fake response/run success. Include JSON upload + URL import, empty state, synthetic demo, split/unified diff, stable anchors, scenario/provenance explanation, full request/output, assertion results and run history, version status, responsive light/dark themes. Baseline can be compared to withdrawn candidates. New candidates edited as JSON patches in GUI and CLI, with rationale.

## Verification and release

Use synthetic fixtures and local HTTP/agent stubs for automated integration; clearly label demo outputs synthetic. Test installed wheel CLI/GUI independently of the source checkout, source normalization, paired inputs/hashes, append-only reviews and replies, duplicates, replay errors/cancel, stale comments, agent failures, local-server boundaries, export/import. These checks do not establish compatibility with a particular live data service. External model calls and publication require user authorization. Public source and release assets exclude private experiment evidence. CI/release configuration defaults to manual registry publication; package ownership must be confirmed before enabling registry updates.
