# The SDK surface the daemon depends on (TypeScript)

Every type, function, method, option, field, key and value of `@anthropic-ai/claude-agent-sdk`
that the source under `src/agent/claude/` reads, calls or decides on, with where each is known
from. A new SDK release is checked against this table before it is pinned.

Read against `@anthropic-ai/claude-agent-sdk` 0.3.296 (bundled Claude Code 2.1.296), its
declarations in `sdk.d.ts` and the
[Agent SDK reference for TypeScript](https://code.claude.com/docs/en/agent-sdk/typescript) on
2026-10-10. This file stands beside [sdk-surface.md](sdk-surface.md), the same table for the Python
source, until the Python tree is removed; it then takes that file's name.

## How to read it

| Column | Meaning |
|---|---|
| Owner | For a `type` or a `function`, the module it is imported from. For anything else, the SDK type it belongs to, or a dotted path below one for what the declarations leave open (`SDKPartialAssistantMessage.event.delta`); `[]` after a name is an element of a list. A function's parameters are its own: `listSessions` owns the fields of its options, `CanUseTool` those of the options it is called with |
| Member | The name, or for a `value` the literal the code compares against or passes, in double quotes |
| Kind | `type` (an imported type or alias), `function`, `method` (of `Query`), `option` (a property the daemon passes in an object the SDK takes: `Options`, a permission result, a prompt), `field` (a property the daemon reads, below a type that `sdk.d.ts` declares), `key` (a property the daemon reads below something the declarations leave open: a stream event, a content block, a tool's result or input) or `value` |
| Used in | The files under `src/agent/claude/` and the symbols that depend on it, as `file: Class.member` |
| Source | `reference`: the reference names it, in the section of its type. `package`: `sdk.d.ts` declares it and the reference does not. `measured`: neither does, and it was read off a real stream |
| Checked by | The probe claim whose text names it, or `none` |

The source reads every record, hook input and result of Claude Code as `unknown` and narrows it
where it is read, so a row below a type is a property of the record as Claude Code writes it,
whether or not the declaration of that type has it.

A `package` or `measured` row is a dependency the documentation does not promise. It can change in
a release with no notice, so it is the first place to look when a release misbehaves.

## What keeps it true

- `test/agent/claude/sdk-surface.test.ts` runs with the test suite. It fails when the source
  imports an SDK name the table does not list or the table lists one the source no longer imports,
  when a `reference` or `package` row names something `sdk.d.ts` does not declare (the member, or
  the type it hangs from), when a `measured` row names something it does declare, when a member no
  longer appears inside any of the symbols its row names, and when a claim in the last column does
  not exist in `probe/claims.py`. `sdk.d.ts` is read as text: a type's block runs from its
  `declare` line to the `;` that closes it, or for an interface to its closing brace, and the
  members of a union or an intersection are those of the types it names.
- The same file fails when the first list under "Types the daemon does not read" differs from
  `UNREAD_KINDS` in `translate.ts`, when a type in either list is not in the SDK's `SDKMessage`
  union or is imported by the source, when that union holds a type that is neither in the table
  nor in a list, and when a recording carries a kind listed as not recorded.
- No check reads the published reference yet. The helpers that would (`inReference`, `check`,
  `report`) are in the test file and run against a small page there, and move to the TypeScript
  probe when it has a surface module. Until then a `reference` row is checked against the page by
  hand on a new release.
- Nothing checks that the table lists every field the source reads: the import check is complete
  for types and functions only. A change that reads a new field, key or value of the SDK adds its
  row in the same pull request.

## On a new SDK release

1. Install the release and run `node --test test/agent/claude/sdk-surface.test.ts`. A failing
   test names the row.
2. For each row whose source test fails, the symbol in its Used in column is what to fix or to stop
   using. A `measured` row that the package now declares moves to `package`, or to `reference`.
3. For each type the test reports as new in the `SDKMessage` union, decide whether the daemon
   should handle it: add its rows, or list it under "Types the daemon does not read".
4. Read the reference sections of the `reference` rows: move a row to `package` or `measured` if
   the reference dropped it, or correct it if the reference renamed it.
5. Run `test/agent/claude/translate.test.ts` on the recordings: it fails on a recorded kind that
   the translator neither reads nor lists.
6. Update the versions and the date at the top of this file.

## The table

| Owner | Member | Kind | Used in | Source | Checked by |
|---|---|---|---|---|---|
| `@anthropic-ai/claude-agent-sdk` | `CanUseTool` | type | requests.ts: CallContext | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `EffortLevel` | type | session.ts: QueryHandle, effortOf | package | none |
| `@anthropic-ai/claude-agent-sdk` | `HookCallback` | type | session.ts: ClaudeSession.#hook | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `Options` | type | session.ts: QueryFunction, ClaudeSession.#options | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `PermissionMode` | type | session.ts: QueryHandle, modeOf | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `PermissionResult` | type | requests.ts: permissionResult, questionResult | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `SDKSessionInfo` | type | info.ts: listedSession | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `SDKUserMessage` | type | prompt.ts: Content, userMessage; session.ts: QueryFunction, ClaudeSession | reference | none |
| `@anthropic-ai/claude-agent-sdk` | `listSessions` | function | listing.ts: directorySessions | reference | P6 |
| `@anthropic-ai/claude-agent-sdk` | `query` | function | session.ts: ClaudeSession.constructor | reference | none |
| `Query` | `applyFlagSettings` | method | session.ts: ClaudeSession.setEffort | reference | none |
| `Query` | `close` | method | session.ts: ClaudeSession.#pump, ClaudeSession.close | reference | none |
| `Query` | `getContextUsage` | method | session.ts: ClaudeSession.contextUsage | reference | P2 |
| `Query` | `initializationResult` | method | session.ts: ClaudeSession.ready, ClaudeSession.info | reference | P2 |
| `Query` | `interrupt` | method | session.ts: ClaudeSession.interrupt | reference | P8 |
| `Query` | `setModel` | method | session.ts: ClaudeSession.setModel | reference | P18 |
| `Query` | `setPermissionMode` | method | session.ts: ClaudeSession.setPermissionMode | reference | P9 |
| `Query` | `stopTask` | method | session.ts: ClaudeSession.stopTask | reference | P12 |
| `HookCallbackMatcher` | `hooks` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `allowDangerouslySkipPermissions` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `canUseTool` | option | session.ts: ClaudeSession.#options | reference | P11 |
| `Options` | `cwd` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `effort` | option | session.ts: ClaudeSession.#options | reference | P16 |
| `Options` | `extraArgs` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `hooks` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `includePartialMessages` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `model` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `permissionMode` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `resume` | option | session.ts: ClaudeSession.#options | reference | P7 |
| `Options` | `settingSources` | option | session.ts: ClaudeSession.#options | reference | none |
| `Options` | `stderr` | option | session.ts: ClaudeSession.#options | reference | none |
| `PermissionResult` | `behavior` | option | requests.ts: permissionResult, questionResult | reference | none |
| `PermissionResult` | `message` | option | requests.ts: permissionResult, questionResult | reference | none |
| `PermissionResult` | `updatedInput` | option | requests.ts: permissionResult, questionResult | reference | none |
| `SDKUserMessage` | `message` | option | prompt.ts: userMessage | reference | none |
| `SDKUserMessage` | `parent_tool_use_id` | option | prompt.ts: userMessage | reference | none |
| `SDKUserMessage` | `type` | option | prompt.ts: userMessage | reference | none |
| `SDKUserMessage` | `uuid` | option | prompt.ts: userMessage | reference | P19 |
| `SDKUserMessage.message` | `content` | option | prompt.ts: contentOf, userMessage | measured | none |
| `SDKUserMessage.message` | `role` | option | prompt.ts: userMessage | measured | none |
| `SDKUserMessage.message.content[]` | `source` | option | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[]` | `text` | option | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[]` | `type` | option | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].source` | `data` | option | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].source` | `media_type` | option | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].source` | `type` | option | prompt.ts: contentOf | measured | none |
| `Settings` | `effortLevel` | option | session.ts: ClaudeSession.setEffort | package | none |
| `listSessions` | `dir` | option | listing.ts: directorySessions | reference | none |
| `listSessions` | `includeWorktrees` | option | listing.ts: directorySessions | reference | none |
| `BaseHookInput` | `cwd` | field | hooks.ts: folderChanged | reference | P14 |
| `BaseHookInput` | `effort` | field | hooks.ts: stopHookEvents | reference | none |
| `BaseHookInput.effort` | `level` | field | hooks.ts: stopHookEvents | package | none |
| `CanUseTool` | `description` | field | requests.ts: toRequest, CallContext | package | none |
| `CanUseTool` | `requestId` | field | session.ts: ClaudeSession.#options | reference | none |
| `CanUseTool` | `title` | field | requests.ts: toRequest, CallContext | package | none |
| `CanUseTool` | `toolUseID` | field | requests.ts: toRequest, CallContext | reference | none |
| `ModelInfo` | `description` | field | info.ts: modelOf | reference | none |
| `ModelInfo` | `displayName` | field | info.ts: modelOf | reference | P17 |
| `ModelInfo` | `supportedEffortLevels` | field | info.ts: modelOf | reference | P17 |
| `ModelInfo` | `supportsEffort` | field | info.ts: modelOf | reference | none |
| `ModelInfo` | `value` | field | info.ts: modelOf | reference | P17 |
| `ModelUsage` | `cacheCreationInputTokens` | field | translate.ts: tokensOf | reference | none |
| `ModelUsage` | `cacheReadInputTokens` | field | translate.ts: tokensOf | reference | none |
| `ModelUsage` | `inputTokens` | field | translate.ts: tokensOf | reference | none |
| `ModelUsage` | `outputTokens` | field | translate.ts: tokensOf | reference | none |
| `SDKAssistantMessage` | `error` | field | translate.ts: assistant | reference | none |
| `SDKAssistantMessage` | `message` | field | translate.ts: assistant | reference | none |
| `SDKAssistantMessage` | `parent_tool_use_id` | field | translate.ts: parentOf | reference | none |
| `SDKCompactBoundaryMessage` | `compact_metadata` | field | translate.ts: compactBoundary | reference | none |
| `SDKCompactBoundaryMessage.compact_metadata` | `post_tokens` | field | translate.ts: compactBoundary | package | none |
| `SDKCompactBoundaryMessage.compact_metadata` | `pre_tokens` | field | translate.ts: compactBoundary | package | none |
| `SDKControlGetContextUsageResponse` | `model` | field | info.ts: contextUsage | reference | none |
| `SDKControlGetContextUsageResponse` | `percentage` | field | info.ts: contextUsage | reference | none |
| `SDKControlInitializeResponse` | `commands` | field | info.ts: agentInfo | reference | none |
| `SDKControlInitializeResponse` | `current_permission_mode` | field | info.ts: agentInfo | measured | none |
| `SDKControlInitializeResponse` | `models` | field | info.ts: agentInfo | reference | P17 |
| `SDKMessage` | `subtype` | field | translate.ts: kindOf | package | none |
| `SDKMessage` | `type` | field | translate.ts: kindOf | package | none |
| `SDKMessageOrigin` | `kind` | field | translate.ts: result | reference | none |
| `SDKPartialAssistantMessage` | `event` | field | translate.ts: kindOf, messageStart, contentBlockStart, contentBlockDelta | reference | none |
| `SDKPartialAssistantMessage` | `parent_tool_use_id` | field | translate.ts: parentOf | reference | none |
| `SDKResultMessage` | `is_error` | field | translate.ts: endingOf; session.ts: ClaudeSession.#pump | reference | none |
| `SDKResultMessage` | `modelUsage` | field | translate.ts: result | reference | none |
| `SDKResultMessage` | `origin` | field | translate.ts: result | reference | none |
| `SDKResultMessage` | `result` | field | translate.ts: result | reference | none |
| `SDKResultMessage` | `session_id` | field | translate.ts: result | reference | P3 |
| `SDKResultMessage` | `terminal_reason` | field | translate.ts: endingOf | reference | none |
| `SDKStatusMessage` | `compact_result` | field | translate.ts: status | package | none |
| `SDKStatusMessage` | `permissionMode` | field | translate.ts: status | reference | none |
| `SDKStatusMessage` | `status` | field | translate.ts: status | reference | P22 |
| `SDKSystemMessage` | `claude_code_version` | field | translate.ts: init | reference | P1 |
| `SDKSystemMessage` | `session_id` | field | translate.ts: init | reference | none |
| `SDKTaskNotificationMessage` | `status` | field | translate.ts: taskNotification | reference | none |
| `SDKTaskNotificationMessage` | `summary` | field | translate.ts: taskNotification | reference | none |
| `SDKTaskNotificationMessage` | `task_id` | field | translate.ts: taskNotification | reference | none |
| `SDKTaskNotificationMessage` | `tool_use_id` | field | translate.ts: taskNotification | reference | none |
| `SDKTaskNotificationMessage` | `usage` | field | translate.ts: taskNotification | reference | none |
| `SDKTaskNotificationMessage.usage` | `duration_ms` | field | translate.ts: taskNotification | package | none |
| `SDKTaskProgressMessage` | `description` | field | translate.ts: taskProgress | reference | none |
| `SDKTaskProgressMessage` | `task_id` | field | translate.ts: taskProgress | reference | none |
| `SDKTaskProgressMessage` | `tool_use_id` | field | translate.ts: taskProgress | reference | none |
| `SDKTaskStartedMessage` | `description` | field | translate.ts: taskStarted | reference | none |
| `SDKTaskStartedMessage` | `task_id` | field | translate.ts: taskStarted | reference | none |
| `SDKTaskStartedMessage` | `task_type` | field | translate.ts: taskStarted | reference | none |
| `SDKTaskStartedMessage` | `tool_use_id` | field | translate.ts: taskStarted | reference | none |
| `SDKTaskUpdatedMessage` | `patch` | field | translate.ts: taskUpdated | reference | none |
| `SDKTaskUpdatedMessage` | `task_id` | field | translate.ts: taskUpdated | reference | none |
| `SDKTaskUpdatedMessage.patch` | `status` | field | translate.ts: taskUpdated | package | none |
| `SDKUserMessage` | `message` | field | translate.ts: user | reference | none |
| `SDKUserMessage` | `parent_tool_use_id` | field | translate.ts: parentOf | reference | none |
| `SDKUserMessage` | `tool_use_result` | field | translate.ts: user | reference | P13 |
| `SDKUserMessageReplay` | `uuid` | field | translate.ts: user | reference | P19 |
| `SlashCommand` | `aliases` | field | info.ts: commandOf | reference | none |
| `SlashCommand` | `argumentHint` | field | info.ts: commandOf | reference | none |
| `SlashCommand` | `description` | field | info.ts: commandOf | reference | none |
| `SlashCommand` | `name` | field | info.ts: commandOf | reference | none |
| `AskUserQuestion.input` | `answers` | key | requests.ts: questionResult | measured | none |
| `AskUserQuestion.input` | `questions` | key | requests.ts: questionsOf, questionResult | measured | none |
| `AskUserQuestion.input.questions[]` | `header` | key | requests.ts: questionOf | measured | none |
| `AskUserQuestion.input.questions[]` | `multiSelect` | key | requests.ts: questionOf | measured | none |
| `AskUserQuestion.input.questions[]` | `options` | key | requests.ts: questionOf | measured | none |
| `AskUserQuestion.input.questions[]` | `question` | key | requests.ts: questionOf | measured | none |
| `AskUserQuestion.input.questions[].options[]` | `description` | key | requests.ts: optionOf | measured | none |
| `AskUserQuestion.input.questions[].options[]` | `label` | key | requests.ts: optionOf | measured | none |
| `AskUserQuestion.input.questions[].options[]` | `preview` | key | requests.ts: optionOf | measured | none |
| `Options.extraArgs` | `chrome` | key | session.ts: ClaudeSession.#options | measured | none |
| `Options.extraArgs` | `replay-user-messages` | key | session.ts: ClaudeSession.#options | measured | P19 |
| `SDKAssistantMessage.message` | `content` | key | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.message` | `id` | key | translate.ts: assistant | measured | P20 |
| `SDKAssistantMessage.message.content[]` | `content` | key | translate.ts: callEnded | measured | none |
| `SDKAssistantMessage.message.content[]` | `id` | key | translate.ts: callStarted | measured | none |
| `SDKAssistantMessage.message.content[]` | `input` | key | translate.ts: callStarted | measured | none |
| `SDKAssistantMessage.message.content[]` | `is_error` | key | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.message.content[]` | `name` | key | translate.ts: callStarted | measured | none |
| `SDKAssistantMessage.message.content[]` | `text` | key | translate.ts: wordsOf | measured | none |
| `SDKAssistantMessage.message.content[]` | `tool_use_id` | key | translate.ts: callEnded | measured | none |
| `SDKAssistantMessage.message.content[]` | `type` | key | translate.ts: assistant, wordsOf | measured | none |
| `SDKAssistantMessage.message.content[].content` | `text` | key | translate.ts: resultText | measured | none |
| `SDKAssistantMessage.message.content[].content` | `type` | key | translate.ts: resultText | measured | none |
| `SDKPartialAssistantMessage.event` | `content_block` | key | translate.ts: contentBlockStart | measured | none |
| `SDKPartialAssistantMessage.event` | `delta` | key | translate.ts: contentBlockDelta | measured | none |
| `SDKPartialAssistantMessage.event` | `message` | key | translate.ts: messageStart | measured | none |
| `SDKPartialAssistantMessage.event` | `type` | key | translate.ts: kindOf | measured | none |
| `SDKPartialAssistantMessage.event.content_block` | `type` | key | translate.ts: contentBlockStart | measured | none |
| `SDKPartialAssistantMessage.event.delta` | `text` | key | translate.ts: contentBlockDelta | measured | none |
| `SDKPartialAssistantMessage.event.delta` | `type` | key | translate.ts: contentBlockDelta | measured | none |
| `SDKPartialAssistantMessage.event.message` | `id` | key | translate.ts: messageStart | measured | P20 |
| `SDKUserMessage.message` | `content` | key | translate.ts: user | measured | none |
| `SDKUserMessage.message.content[]` | `content` | key | translate.ts: callEnded | measured | none |
| `SDKUserMessage.message.content[]` | `id` | key | translate.ts: callStarted | measured | none |
| `SDKUserMessage.message.content[]` | `input` | key | translate.ts: callStarted | measured | none |
| `SDKUserMessage.message.content[]` | `is_error` | key | translate.ts: user | measured | none |
| `SDKUserMessage.message.content[]` | `name` | key | translate.ts: callStarted | measured | none |
| `SDKUserMessage.message.content[]` | `tool_use_id` | key | translate.ts: callEnded | measured | none |
| `SDKUserMessage.message.content[]` | `type` | key | translate.ts: user | measured | none |
| `SDKUserMessage.message.content[].content` | `text` | key | translate.ts: resultText | measured | none |
| `SDKUserMessage.message.content[].content` | `type` | key | translate.ts: resultText | measured | none |
| `SDKUserMessage.tool_use_result` | `content` | key | translate.ts: fileChange | measured | none |
| `SDKUserMessage.tool_use_result` | `filePath` | key | translate.ts: fileChange | measured | none |
| `SDKUserMessage.tool_use_result` | `structuredPatch` | key | translate.ts: fileChange | measured | none |
| `SDKUserMessage.tool_use_result` | `type` | key | translate.ts: fileChange | measured | none |
| `SDKUserMessage.tool_use_result.structuredPatch[]` | `lines` | key | translate.ts: hunkOf | measured | none |
| `SDKUserMessage.tool_use_result.structuredPatch[]` | `newStart` | key | translate.ts: hunkOf | measured | none |
| `SDKUserMessage.tool_use_result.structuredPatch[]` | `oldStart` | key | translate.ts: hunkOf | measured | none |
| `CanUseTool.toolName` | `"AskUserQuestion"` | value | requests.ts: QUESTION_TOOL | measured | none |
| `EffortLevel` | `"high"` | value | session.ts: EFFORT_LEVELS | package | none |
| `EffortLevel` | `"low"` | value | session.ts: EFFORT_LEVELS | package | none |
| `EffortLevel` | `"max"` | value | session.ts: EFFORT_LEVELS | package | none |
| `EffortLevel` | `"medium"` | value | session.ts: EFFORT_LEVELS | package | none |
| `EffortLevel` | `"xhigh"` | value | session.ts: EFFORT_LEVELS | package | none |
| `HookEvent` | `"PostToolUse"` | value | session.ts: ClaudeSession.#options | reference | none |
| `HookEvent` | `"Stop"` | value | session.ts: ClaudeSession.#options | reference | none |
| `PermissionMode` | `"acceptEdits"` | value | session.ts: PERMISSION_MODES | reference | none |
| `PermissionMode` | `"auto"` | value | capabilities.ts: CAPABILITIES; session.ts: PERMISSION_MODES | reference | none |
| `PermissionMode` | `"bypassPermissions"` | value | capabilities.ts: CAPABILITIES; session.ts: PERMISSION_MODES | reference | none |
| `PermissionMode` | `"default"` | value | capabilities.ts: CAPABILITIES; session.ts: PERMISSION_MODES | reference | none |
| `PermissionMode` | `"dontAsk"` | value | session.ts: PERMISSION_MODES | reference | none |
| `PermissionMode` | `"plan"` | value | session.ts: PERMISSION_MODES | reference | none |
| `PermissionResult.behavior` | `"allow"` | value | requests.ts: permissionResult, questionResult | reference | none |
| `PermissionResult.behavior` | `"deny"` | value | requests.ts: permissionResult, questionResult | reference | none |
| `SDKAssistantMessage.message.content[].type` | `"advisor_tool_result"` | value | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.message.content[].type` | `"server_tool_use"` | value | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.message.content[].type` | `"text"` | value | translate.ts: wordsOf | measured | none |
| `SDKAssistantMessage.message.content[].type` | `"tool_result"` | value | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.message.content[].type` | `"tool_use"` | value | translate.ts: assistant | measured | none |
| `SDKAssistantMessage.type` | `"assistant"` | value | translate.ts: HANDLERS | reference | none |
| `SDKAssistantMessageError` | `"authentication_failed"` | value | translate.ts: AUTHENTICATION_FAILED, assistant | package | none |
| `SDKCompactBoundaryMessage.subtype` | `"compact_boundary"` | value | translate.ts: HANDLERS | reference | P21 |
| `SDKMessageOrigin.kind` | `"human"` | value | translate.ts: result | reference | none |
| `SDKPartialAssistantMessage.event.content_block.type` | `"text"` | value | translate.ts: contentBlockStart | measured | none |
| `SDKPartialAssistantMessage.event.delta.type` | `"text_delta"` | value | translate.ts: contentBlockDelta | measured | none |
| `SDKPartialAssistantMessage.event.type` | `"content_block_delta"` | value | translate.ts: HANDLERS | measured | none |
| `SDKPartialAssistantMessage.event.type` | `"content_block_start"` | value | translate.ts: HANDLERS | measured | none |
| `SDKPartialAssistantMessage.event.type` | `"message_start"` | value | translate.ts: HANDLERS | measured | P20 |
| `SDKPartialAssistantMessage.event.type` | `"message_stop"` | value | translate.ts: HANDLERS | measured | none |
| `SDKPartialAssistantMessage.type` | `"stream_event"` | value | translate.ts: kindOf | reference | none |
| `SDKRateLimitEvent.type` | `"rate_limit_event"` | value | translate.ts: HANDLERS | reference | none |
| `SDKResultMessage.terminal_reason` | `"aborted_streaming"` | value | translate.ts: INTERRUPTED | reference | none |
| `SDKResultMessage.terminal_reason` | `"aborted_tools"` | value | translate.ts: INTERRUPTED | reference | none |
| `SDKResultMessage.type` | `"result"` | value | translate.ts: HANDLERS; session.ts: ClaudeSession.#pump | reference | none |
| `SDKStatusMessage.status` | `"compacting"` | value | translate.ts: status | reference | P22 |
| `SDKStatusMessage.subtype` | `"status"` | value | translate.ts: HANDLERS | reference | none |
| `SDKSystemMessage.subtype` | `"init"` | value | translate.ts: HANDLERS | reference | P1 |
| `SDKSystemMessage.type` | `"system"` | value | translate.ts: kindOf | reference | none |
| `SDKTaskNotificationMessage.subtype` | `"task_notification"` | value | translate.ts: HANDLERS | reference | none |
| `SDKTaskProgressMessage.subtype` | `"task_progress"` | value | translate.ts: HANDLERS | reference | none |
| `SDKTaskStartedMessage.subtype` | `"task_started"` | value | translate.ts: HANDLERS | reference | none |
| `SDKTaskStartedMessage.task_type` | `"local_agent"` | value | translate.ts: TASK_KINDS | reference | none |
| `SDKTaskStartedMessage.task_type` | `"local_bash"` | value | translate.ts: TASK_KINDS | reference | none |
| `SDKTaskUpdatedMessage.patch.status` | `"completed"` | value | translate.ts: TERMINAL_TASK_STATUSES | package | none |
| `SDKTaskUpdatedMessage.patch.status` | `"failed"` | value | translate.ts: TERMINAL_TASK_STATUSES | package | none |
| `SDKTaskUpdatedMessage.patch.status` | `"killed"` | value | translate.ts: TERMINAL_TASK_STATUSES | package | none |
| `SDKTaskUpdatedMessage.patch.status` | `"stopped"` | value | translate.ts: TERMINAL_TASK_STATUSES | measured | none |
| `SDKTaskUpdatedMessage.subtype` | `"task_updated"` | value | translate.ts: HANDLERS | reference | none |
| `SDKUserMessage.message.content[].source.type` | `"base64"` | value | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].type` | `"image"` | value | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].type` | `"text"` | value | prompt.ts: contentOf | measured | none |
| `SDKUserMessage.message.content[].type` | `"tool_result"` | value | translate.ts: user | measured | none |
| `SDKUserMessage.message.content[].type` | `"tool_use"` | value | translate.ts: user | measured | none |
| `SDKUserMessage.message.role` | `"user"` | value | prompt.ts: userMessage | measured | none |
| `SDKUserMessage.tool_use_result.type` | `"create"` | value | translate.ts: fileChange | measured | none |
| `SDKUserMessage.type` | `"user"` | value | translate.ts: HANDLERS; prompt.ts: userMessage | reference | none |

## Types the daemon does not read

A record of a kind the translator does not match gives no event, and nothing in the reply or in
the session depends on it. The first list is `UNREAD_KINDS` in `translate.ts`: kinds that the
recordings under `tests/fixtures/sdk/` carry. The second is every other type of the `SDKMessage`
union, none of which any recording carries.

### Recorded and left unread

- `command_lifecycle`: in no declaration of `sdk.d.ts`; the queue of a prompt sent while a turn runs
- `conversation_reset`: `SDKConversationResetMessage`
- `stream_event:content_block_stop`: a stream event of the Claude API, below `SDKPartialAssistantMessage.event`
- `stream_event:message_delta`: a stream event of the Claude API, below `SDKPartialAssistantMessage.event`
- `system:api_retry`: `SDKAPIRetryMessage`
- `system:background_tasks_changed`: `SDKBackgroundTasksChangedMessage`
- `system:thinking_tokens`: `SDKThinkingTokensMessage`
- `tool_progress`: `SDKToolProgressMessage`

### Declared and not recorded

- `SDKAuthStatusMessage`
- `SDKCommandsChangedMessage`
- `SDKControlRequestProgressMessage`
- `SDKElicitationCompleteMessage`
- `SDKFilesPersistedEvent`
- `SDKHookProgressMessage`
- `SDKHookResponseMessage`
- `SDKHookStartedMessage`
- `SDKInformationalMessage`
- `SDKLocalCommandOutputMessage`
- `SDKMemoryRecallMessage`
- `SDKMirrorErrorMessage`
- `SDKModelRefusalFallbackMessage`
- `SDKModelRefusalNoFallbackMessage`
- `SDKNotificationMessage`
- `SDKPermissionDeniedMessage`
- `SDKPluginInstallMessage`
- `SDKPromptSuggestionMessage`
- `SDKSessionStateChangedMessage`
- `SDKToolUseSummaryMessage`
- `SDKWorkerShuttingDownMessage`

## Dependencies the table cannot hold

These are not names the declarations or the reference define, so no row can express them and
no check covers them here.

- `ClaudeSession.#pump` in `session.ts` reads the `Query` as an async iterable of whatever Claude
  Code writes. The declaration is `AsyncGenerator<SDKMessage, void>`, which describes less than the
  wire carries. Measured on 2026-10-10 on the recordings under `tests/fixtures/sdk/` against
  `sdk.d.ts` 0.3.296: of the 23 kinds they hold, `command_lifecycle` is in no declaration and 10
  others carry fields that the declaration of their type lacks (`thinking_display` on three stream
  events, `user_message_uuids` on `status`, `logical_parent_uuid` on `compact_boundary`, and more).
- The rows under `SDKPartialAssistantMessage.event` are events of the Claude API, typed by
  `BetaRawMessageStreamEvent` of `@anthropic-ai/sdk` 0.133.0, which `sdk.d.ts` imports and does not
  declare. The same holds for the rows under `SDKAssistantMessage.message` and
  `SDKUserMessage.message` (`BetaMessage` and `MessageParam`), whose content blocks the daemon
  reads and writes. Two block types in the table, `"server_tool_use"` and `"advisor_tool_result"`,
  are declared in `@anthropic-ai/sdk` and carried by no recording, so their rows are `measured`
  only in the sense that no declaration of the package names them.
- The rows under `AskUserQuestion.input` describe the input of a Claude Code tool, which the SDK
  hands to `canUseTool` as a plain record. The reference lists it under Tool Input Types and the
  package declares it in `sdk-tools.d.ts`; the checks of this table read neither, so the rows stand
  on the recordings `ask.jsonl` and `ask-can-use-tool.json`.
- `"stopped"` among the values of `SDKTaskUpdatedMessage.patch.status` is in the translator's
  `TERMINAL_TASK_STATUSES`, taken from the Python SDK's list. The declaration of `patch.status`
  lacks it and the recordings show `"completed"` and `"failed"` only.
- `SDKControlInitializeResponse.current_permission_mode` is in `server-info.json` and in no
  declaration. `CanUseTool.title` is declared and was absent in both permission calls measured on
  SDK 0.3.296 on 2026-10-10. `"human"` of `SDKMessageOrigin.kind` is in no recording: the results
  of the owner's own prompts carry no `origin` there.
- `listing.ts` derives the folder of a folder's transcripts by a rule read in the package's
  `sdk.mjs` 0.3.296, which nothing documents. `test/agent/claude/listing.test.ts` writes a
  transcript where the rule puts it and has the SDK's own `listSessions` find it. `lastMessageMs`
  in `listing.ts` reads the `type` and `timestamp` of the transcript's lines, a format the
  documentation does not describe.
- `trust.ts: workspaceTrusted` reads `projects["<path>"].hasTrustDialogAccepted` and
  `chrome.ts: chromeEnabled` reads `claudeInChromeDefaultEnabled`, both in Claude Code's own
  `~/.claude.json`. They are records of the CLI, not names of the SDK.
- `usage.ts: parseUsage` reads the wording Claude Code writes in the result of `/usage`, which the
  usage probe in `usage-probe.ts` sends as a prompt. The wording belongs to the CLI.
- The two keys under `Options.extraArgs` are flags of the CLI that the SDK forwards.
