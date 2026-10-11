# v0.3-alpha.1 implementation contract

Application/course release: `0.3-alpha.1`; persisted state version: `3`; export format version: `3`. The eight mathematically unchanged v0.2 questions retain question version `0.2-alpha.1`. Question identity, practice run, assessment run, active instance, and page position are separate concepts. Historical answers keep their original grader version and verdict.

## Content and public state

`course.json` contains `{id, version, title, scope, school, term, applicability, source_priority, sources, units, foundation, questions}`. `units[]` use `basic`, `substitution`, `parts`, `definite`, with a lesson/example, prerequisites and four explicit role orders. Each unit has 1 diagnostic, 4 practice, 2 post and 1 delayed question. Questions add stable opaque `ref`, per-question `version`, `title`, `difficulty`, `minutes`, `pool` and `source_refs` to the previous mathematical schema. Definite questions include `lower` and `upper`.

`pool` is `practice`, `diagnostic`, `post` or `delayed`; practice `phase` is guided or transfer. Only 16 practice summaries are available through catalog, filters or selection. Assessment pools and their future formulas/answers are never sent in bulk. Sources in this release are all original: no school-specific frequency, score weight or exam probability is inferred.

The v3 state response contains:

- `version`, `revision`, `participant`, `csrf`, `phase`, `minutes`, `course`, `legacy_mode`.
- `instance_id`, `run_id`, `question`, `progress`, `feedback`, `last_answer`, `hints`, `hint_count`, `help_used`, `chat`, `lesson`, `solution`.
- `catalog` with practice summaries, skill availability, `diagnostic_state` and `diagnostic_label`; `recommendation`, `review`, `budget`; `report` only on the report page.
- `assessment` for the active assessment; `export_allowed`; `legacy_drafts` for exact browser-key mappings.
- `ai_enabled`, `ai_request`, `ai_limits`; service configuration fills AI availability and quota values. Availability means configured, not verified with the provider.

V3 phases are `setup`, `catalog`, `training`, `assessment`, `review`, `report`. An active question uses its opaque reference, question version and per-run id; re-entering can rotate the opaque instance without destroying its evidence or draft. Question status codes are `unseen`, `in_progress`, `needs_review`, `assisted`, `retry_complete`, `independent`, `ungraded`. Assistance on later attempts does not overwrite a genuinely independent first attempt.

While assessing, state excludes catalog, recommendation, review, budget, hints, chat, lesson, solution, report, last submitted answer and current verdict. The current question title is generic. Only the displayed question is marked seen; abandoning an assessment never restores its unseen status. Model context is never built for assessment questions.

## HTTP and actions

All POST routes require JSON, the current session Cookie and `X-CSRF-Token`; body limit is 16,000 bytes. State mutations require matching integer `revision`. Current-question actions also require the current `instance_id`. Successful actions return updated state; stale revision/instance or conflicting SQLite snapshot returns `409`, invalid action/input generally `422`, session/CSRF failures `403`. Failed mutations do not persist partial state.

| Route | Request / result |
|---|---|
| `GET /api/state` | Creates/restores the local session; returns the current public state. Supported migration and expired AI cleanup may be persisted during the read. |
| `GET /api/catalog` | Existing session required; returns practice summaries and skill metadata only. Returns `409` during an assessment. |
| `POST /api/action` | `{action, revision, instance_id?, ...}`; actions below. |
| `POST /api/preview` | `{instance_id, expression}`; no revision required. Parse-only result `{valid:true,mathml}` or `{valid:false,message}`, maximum 180 characters. Available for the current training/assessment instance. |
| `POST /api/tutor` | `{revision, instance_id, message}`; validates and durably reserves quota, returns `202` with pending public state. Poll `/api/state` for completion. |
| `GET /api/export` | Export-local references and happened records only; `409` during any active assessment. No credentials, unpublished answers or browser drafts. |
| `POST /api/reset` | `{revision}`; replace current participant/state and rotate CSRF while retaining the Cookie session id; refuses a pending AI request. |
| `GET /api/health` | `{ok,version,ai_enabled,local_only}`; no provider request. |

V3 actions:

| Action | Additional fields and behavior |
|---|---|
| `start` | `minutes` integer 15–240; optional `diagnostic:true` begins the four-question diagnostic, otherwise opens catalog. |
| `budget` | `minutes` integer 15–240; updates the current plan. Optional `new_session:true` starts a fresh time budget without clearing learning evidence. |
| `select` | `question_ref` from the public practice catalog. Does not permit reserved-question selection. |
| `catalog`, `review`, `report` | Navigate without resetting practice evidence. Opening report records exposure of reference answers for attempted questions. |
| `answer`, `skip` | `answer` for answer; record current question submission, or skip. Invalid syntax does not append an attempt. Assessment advances without interim grading feedback. |
| `next` | From feedback, move to another recommended practice question. |
| `restart_question` | Begin a new run of only the current practice question; retain all historical evidence. |
| `knowledge`, `hint`, `example`, `solution` | Reveal the requested teaching material and record assistance. |
| `preset` | `message` matching a supported preset; offline assistance. |
| `foundation`, `return_training` | Visit the basic practice question and return to the saved original question. |
| `diagnostic` | Optional `skill`; otherwise use all still-unseen diagnostic questions. |
| `assess` | `skill`, `mode:post` or `mode:delayed`; requires pool availability. |
| `end_assessment` | Explicitly end the active assessment; no invented answer for the unfinished item. |
| `cancel_ai` | Matching `request_id`; cancel local waiting and reject any later result. |

All mutations use revision checks. `answer`, `skip`, `next`, `restart_question`, teaching actions, foundation navigation and `end_assessment` require instance matching. Navigating, selecting, budgeting or starting assessment from training also requires the current instance. In assessment only answer, skip and explicit end are allowed. Legacy actions remain handled only for frozen v2 sessions; compatibility aliases in the engine are not new client API entrypoints.

## Evidence, scheduling and persistence

Each practice run has its own 10-valid-submission allowance, hints, chat and feedback. Lifetime attempts and question-version evidence survive restarts. Missing an independent `+C` gives mathematical verdict correct but normalized outcome incomplete; retries cannot rewrite the original attempt. Unsupported equivalence/domain proofs are ungraded, never counted as wrong. Preview has no grading or domain-solving side effect.

Recommendations are deterministic: resume unfinished work, address latest assessment gaps or lack of independent evidence, respect basic prerequisites, then cover untested areas and consolidate. Same-priority work uses source priority, estimated duration and stable course ordering. Only original content exists now. Unknown outcomes are separately identified. Students can choose any public practice question.

Budget is 15–240 estimated minutes, charged once per question per practice/assessment run rather than wall time. A full plan reserves post-test time for selected skills and a summary; a short plan is explicit if that does not fit. It is not an examination score or completion guarantee.

Immediate post assessment requires two distinct practice questions in that skill to have valid attempt records, or an independently complete diagnostic result. Each skill has two reserved post questions. Delayed assessment unlocks 24 hours after the first completed post run and uses one reserved question. A delivered/assisted/attempted/exposed question is not unseen. Repeating an original practice never replenishes assessment pools.

SQLite `sessions` stores serialized snapshots; save is a conditional update on session id and the exact bytes read. Local locks and cross-process compare-and-swap prevent stale writes. Math workers and provider calls do not hold a SQLite transaction; provider network calls also do not hold session locks. Default database is `app/data/lingkao-alpha.sqlite3`; override using `LINGKAO_DB_PATH`, preferably an absolute path. Cookie name remains `lingkao_alpha_session` and is not port-isolated.

V2 migration is idempotent and does not regrade. It preserves identity, attempts, original grader verdicts, evidence and exposure; matches only unchanged mathematical questions. Unknown historical records are read-only. An in-progress v2 diagnostic/post remains on `legacy_engine.py` plus `legacy_course.json` until complete or explicitly ended; only then does it migrate. Stop old processes before upgrading; rollback uses a pre-upgrade backup, not the migrated database.

Drafts stay in localStorage under participant + question reference + question version + run id, with independent edit ids and revision conflict markers. Successful submit invalidates only its captured edit; one question restart does not delete other question drafts. Same-question interrupted input can be restored; other conflict copies are copy-only in sessionStorage. Storage failure warns without blocking work. Exact v2 key mappings migrate old drafts; unmapped drafts remain copyable instead of being inserted into a different question. Drafts are not exportable learning evidence.

## AI lifecycle and configuration

`ai_tutor.py` calls `https://api.deepseek.com/chat/completions` with server-only key, configurable model (default `deepseek-flash`), `stream:false`, `max_tokens:1000`, and `thinking:{type:disabled}`. It uses only known keys from `app/.env`, with process environment taking precedence. Empty key disables online calls; `.env.example` carries no secret.

Input is 1–600 characters. Context is limited to the current practice's expression, definite bounds where applicable, reviewed answer/steps, related knowledge, recent submission and latest six chat messages. No full catalog, future assessment content or other student's data. Replies are plain text, not executed HTML; the model never assigns verdicts or mutates the plan.

AI request statuses are `pending`, `completed`, `failed`, `cancelled`, `expired`. While pending, the frontend permits draft editing, preview and polling; server mutations are locked except `cancel_ai`. Pending requests bind participant, course, question version, instance and run. Changed identity, cancellation, timeout or late provider completion cannot apply a stale reply. Successful saved replies alone add assistance; failures do not reduce prior independent evidence.

Default deadline is 45 seconds; no automatic retry. Restart does not reissue a pending request; expiry unblocks the session. Cancellation ends local waiting but cannot guarantee provider cancellation. State and quota reservation use one atomic transaction before launching the worker. Durable tables `ai_participants`, `ai_daily`, `ai_reservations` enforce defaults of 30 calls per anonymous participant, 100 per database per UTC day and 2 in flight, with minimum four seconds between participant requests. Accepted failures/cancellations remain charged. Separate databases have separate limits; clearing a participant does not reset the database daily total.

Configuration names: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `LINGKAO_AI_PARTICIPANT_LIMIT`, `LINGKAO_AI_DAILY_LIMIT`, `LINGKAO_AI_CONCURRENCY`, `LINGKAO_AI_TIMEOUT_SECONDS`. Out-of-range limits fail configuration validation. Provider errors are sanitized; keys and raw provider error bodies are not returned to clients.

Only mocked-provider validation is included in this delivery; live four-skill DeepSeek acceptance remains unverified without a valid configured key. Test counts, browser acceptance and remaining limitations are maintained in [QA.md](QA.md).

## Archived v0.2 contract

The following frozen notes document the previous release and its compatibility engine. They do not describe v3 catalog, AI, state or draft behavior.


### 新一次时间安排

`budget` 可选 `new_session: true`；只有 JSON 布尔值 true 会清零本次估时并允许题目在本次再次计时。普通修改分钟数保留已用估时。两种操作都保留题目／练习轮次、草稿定位、原始作答、辅助及暴露历史；测评中和 AI 等待中不可执行。


<details>
<summary>v0.2-alpha.3 contract (historical)</summary>

# v0.2-alpha.3 implementation contract

Application release version is 0.2-alpha.3; course version remains 0.2-alpha.1 and stored state version remains 2 so prior sessions keep their evidence and cursor. Historical attempts retain their recorded grader version and verdict; upgrading does not regrade them.

Alpha.3 persistence: load the exact serialized SQLite snapshot along with the state, then save with one conditional UPDATE matching both session ID and that original snapshot. A changed/deleted snapshot raises 409 without recording the operation. This applies to every mutation, including tutor and reset, across independent servers/processes; per-session locks still serialize local requests. Creation uses INSERT only. No schema migration is needed. Stop all old service processes before upgrading, since older unconditional writers do not enforce this protection.

Alpha.3 math: numeric constants are constructed exactly from source tokens, never Python float values; maximum 40 mantissa digits and scientific exponent ±100, with existing magnitude/expression/resource limits retained. Preserve domain obligations from original logarithms, roots, divisions, negative/fractional powers and tan even if simplification cancels them. Only proven real-domain validity allows correct; violations return incorrect, uncertain domains return ungraded. The current course supports integrands defined on all real x; interval-restricted future integrands conservatively return ungraded until an interval contract is added. Preview parses only and does not solve domains or grade.

Alpha.2 additions: public `last_answer` is null except when a training question has feedback from the current round; it contains the learner's most recent submitted expression, never a reference answer. Assessment, lesson, plan and report responses leave it null. Report rows add `attempts: [{number,answer,outcome,assisted,independent_complete,hints_used}]`, `help_used`, and `help_summary` (human-readable strings). Attempt assistance is a submission-time snapshot; question assistance includes help requested later. Report answer exposure alone is not a requested help action. Foundation help records a durable `foundation_viewed` flag.

The 10-submission limit is per question and training round. `round_attempt_start` is the attempts-array offset at restart, defaults to 0 for old sessions, and does not alter the lifetime records used for first-independent evidence.

Browser drafts are local only and not evidence. They use a participant/course/phase/stage/position key, with a separate foundation slot. Each edit has an independent edit_id; successful answer/skip writes an invalidation marker for only that captured edit, never replaces a newer shared draft. Rejected stale input is retained separately in sessionStorage for that tab; same-question, incomplete inputs may be restored explicitly, all others can only be copied. Reset/restart invalidates participant copies with a revision boundary; reset removes that participant's stored draft text, restart preserves drafts written after the committed new revision. Credentials and unseen content must never enter browser storage. A revision marker prevents older tabs from overwriting newer progress. Storage failure must not block learning and must warn about reload loss. Assessment skips require client confirmation; all server instance/revision validation still applies.

Local only; online AI is always disabled. Course schema: id, version, title, scope, unit {id,name,goal,minutes,rule,boundary,knowledge:[{title,text}],example:{kind,integrand,answer,question_html,answer_html,steps}}, foundation {title,knowledge:[{title,text}],question_id}, questions (original schema + solution_steps), diagnostic_order, training_order, post_order. Main order has 2 guided + 2 transfer. Foundation is separate.

engine exports ROOT, COURSE, QUESTIONS, VERSION, ActionError(message,status=422), new_state(), public(state), action(state,payload), tutor(state,payload), preview(state,payload), export_state(state). No server imports inside engine. All engine mutations enforce payload.revision == state.revision and increment revision once on success. Exception => server must not save mutated state. ActionError.status is HTTP code; stale revision/instance is 409.

State response always has version, revision, participant, csrf, phase (setup/diagnostic/plan/training/post/report), minutes, course {id,version,title,scope}, ai_enabled:false, export_allowed, instance_id (opaque or null), question (null or {instance_id,kind,formula_html,source}), progress {current,total,label}, stage (lesson/guided/transfer/foundation or null), feedback (null or grader result), hints [], hint_count, help_used, chat [], lesson (null or unit lesson object), solution (null or {steps,answer_html}), foundation_info (null or {title,knowledge}), plan (null or {started,completed,can_resume,post_done,reason}), report (null or object).

Assessment responses strip teaching data: hints empty, hint_count=0, feedback/lesson/solution/foundation_info/plan/report null, chat empty, progress label generic. No internal qid, current verdict, all-question lists or example. Course metadata is fine (single-unit assessment is explicitly NOT mixed-method testing).

Report object: diagnostic, transfers, post each {total,complete,independent_complete}; rows [{stage,label,formula_html,first_outcome,latest_outcome,answer,attempt_count,assisted,independent_complete,answer_html}], next_step. Outcomes: correct/incomplete/incorrect/skipped/ungraded. Rows contain only attempted questions; reference answers on report are marked exposed for future attempts. Report is returned only in report phase.

POST /api/action {action,revision,instance_id?,answer?,minutes?}. Actions: start; start_skill (resume saved cursor); begin_practice; answer; skip; next; hint; example; solution; plan; foundation; return_training; restart_training; post; report. All current-question actions including begin_practice, answer/skip/next/hint/example/solution/foundation/return_training require instance_id. plan requires instance_id while training to avoid old tabs exiting new questions. start_skill/restart_training/post/report require revision only. post allowed after main training completed and only once.

POST /api/tutor {revision,instance_id,message}; engine tutor delegates action tutor. Exact supported presets: 第一步怎么选？, 这个方法什么时候不能用？, 怎样检查结果？. No free-text external calls.

POST /api/preview {instance_id,expression}; read-only, no revision required. Returns {valid:true,mathml} or {valid:false,message}. Parse only, no grading. Current instance required. 180 char cap, isolated UTF-8 process and time/resource restrictions.

All successful mutations return state. GET /api/state returns state. GET /api/export returns versioned happened records, never csrf/session/instance tokens/unseen reference answers; diagnostic/post =>409. POST /api/reset {revision} requires CSRF, creates new participant and token, revision=old+1; old identity is deleted. Preserve cookie SID but rotate CSRF. GET /api/health returns version, ai_enabled:false, local_only:true. Cookie lingkao_alpha_session. Default DB app/data/lingkao-alpha.sqlite3; override LINGKAO_DB_PATH for tests. POST CSRF validated under per-session lock against freshly loaded state, including reset.

Export format version 2 replaces all internal qid fields and course-version-plus-qid evidence keys with export-local question_ref values, user-readable question_label and course_version. Attempts, evidence and question-associated events use the same reference within one export. Current labels follow configured order (诊断1、引导练习1、独立变式1、基础补练1、独立复测1); only happened records are included, with no unvisited-question catalog. Historical course versions or unknown historical questions use 历史题目N labels without looking up current answers. Export is read-only and remains available for version-mismatched records outside assessment phases.

Every delivered question has an opaque instance persisted on refresh; switching/re-entering rotates it. Persistent evidence keyed by course version + qid survives instances. Invalid syntax never appends an attempt. +C missing => verdict correct, answer_complete false, normalized incomplete, retry allowed but first attempt remains incomplete. Attempts count skipping and ungraded; preview does not count. Successful answer/hint etc increments revision so duplicate concurrent old POST is rejected. Return to plan preserves main cursor and foundation substate; resuming restores question without erasing help. Explicit restart starts main sequence with retained evidence. Foundation doesn't advance main sequence or independent counts. Training completion means flow completed, not mastery.

</details>
