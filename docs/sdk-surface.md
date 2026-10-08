# The SDK surface the daemon depends on

Every type, function, method, option, field, key and value of `claude-agent-sdk` that the source
under `src/code_with_slack/` reads, calls or decides on, with where each is known from. A new SDK
release is checked against this table before it is pinned.

Read against claude-agent-sdk 0.2.164 (bundled Claude Code 2.1.292) and the
[Agent SDK reference for Python](https://code.claude.com/docs/en/agent-sdk/python) on 2026-10-07.

## How to read it

| Column | Meaning |
|---|---|
| Owner | For a `type` or a `function`, the module it is imported from. For anything else, the SDK type it belongs to, or a dotted path below one for what a `dict[str, Any]` of the SDK holds (`StreamEvent.event.delta`) |
| Member | The name, or for a `value` the literal the code compares against, in double quotes |
| Kind | `type` (an imported class, alias or constant), `function`, `method`, `option` (a keyword the daemon passes), `field`, `key` (of a dict) or `value` |
| Used in | The files and symbols under `src/code_with_slack/` that depend on it |
| Source | `reference`: the reference names it, in the section of its type. `package`: the installed package defines it and the reference does not. `measured`: neither does, and it was read off a real stream |
| Checked by | The probe claim whose text names it, or `none` |

A `package` or `measured` row is a dependency the documentation does not promise. It can change
in a release with no notice, so it is the first place to look when a release misbehaves.

## What keeps it true

- `tests/test_sdk_surface.py` runs with the test suite, so also in the daily SDK release watch on
  the newest release. It fails when the source imports an SDK name the table does not list or the
  table lists one the source no longer imports, when a `reference` or `package` row names
  something the installed package does not define (the member, or the type it hangs from), when
  a `measured` row names something the package does define, when a member no longer appears
  inside any of the symbols its row names, and when a claim in the last column does not exist.
- The probe (`uv run python -m probe`, see [CONTRIBUTING.md](../CONTRIBUTING.md)) checks the
  table before its scenes, on the release it runs on. A row the package no longer defines is
  BROKEN and the release is not certified. A `reference` row the reference no longer names, or a
  reference that cannot be read, is UNPROVEN and is listed to be read by hand. The reference
  counts as naming a member when the member's name appears in the section of its type, in the
  definition or in the prose, so a row is read by hand when the section is rewritten. The check
  runs when the probe runs its scenes, so not on a release that is already certified. It also lists the
  message, block and event types the package exports that the table does not know, and counts
  the rows that are in no reference and under no claim. Those rows do not change from one
  release to the next, so a run does not list them: `uv run python -m probe --surface` does, and
  checks the table alone, with no session and no token.
- Nothing checks that the table lists every field the source reads: the import check is complete
  for types and functions only. A change that reads a new field, key or value of the SDK adds
  its row in the same pull request.

## On a new SDK release

1. Run the probe on it and read the surface section of its output first: three lines when
   nothing changed.
2. For each BROKEN row, the symbol in its Used in column is what to fix or to stop using.
3. For each UNPROVEN row, read the reference: move the row to `package` or `measured` if the
   reference dropped it, or correct the row if the reference renamed it.
4. For each type the package exports and the table does not list, decide whether the daemon
   should handle it.
5. Update the versions and the date at the top of this file.

## The table

| Owner | Member | Kind | Used in | Source | Checked by |
|---|---|---|---|---|---|
| `claude_agent_sdk` | `AssistantMessage` | type | sessions.py: TURN_MESSAGES, ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed, TurnRenderer._assistant | reference | none |
| `claude_agent_sdk` | `ClaudeAgentOptions` | type | sessions.py: ClientFactory, client_options; footer.py: UsageProbe.__init__ | reference | none |
| `claude_agent_sdk` | `ClaudeSDKClient` | type | sessions.py: default_client_factory | reference | none |
| `claude_agent_sdk` | `Message` | type | sessions.py: ClaudeClient.receive_messages, ThreadSession._dispatch, ThreadSession._standalone, ThreadSession.__init__; render/renderer.py: TurnRenderer.feed | reference | none |
| `claude_agent_sdk` | `ResultError` | type | sessions.py: ThreadSession.ensure_connected | reference | none |
| `claude_agent_sdk` | `ResultMessage` | type | sessions.py: TURN_MESSAGES, injected_turn, ThreadSession._dispatch, ThreadSession._finish, ThreadSession._settle; footer.py: UsageProbe.__call__, session_tokens; render/renderer.py: TurnRenderer.feed, TurnRenderer.result | reference | none |
| `claude_agent_sdk` | `SDKSessionInfo` | type | sessions.py: directory_sessions, SessionDeps.sessions_of, SessionManager.sessions_in, SessionManager.dated; resume.py: matching, by_last_activity, _row; home.py: Home.__init__; slack_app.py: resume_into_thread, show_resumed | reference | none |
| `claude_agent_sdk` | `StreamEvent` | type | sessions.py: TURN_MESSAGES; render/renderer.py: TurnRenderer.feed | reference | none |
| `claude_agent_sdk` | `SystemMessage` | type | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | reference | none |
| `claude_agent_sdk` | `UserMessage` | type | sessions.py: TURN_MESSAGES, ThreadSession._acknowledge, ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | reference | none |
| `claude_agent_sdk.types` | `CanUseTool` | type | sessions.py: client_options | reference | none |
| `claude_agent_sdk.types` | `EffortLevel` | type | sessions.py: VALID_EFFORT_LEVELS, client_options, ThreadSession._connect | reference | none |
| `claude_agent_sdk.types` | `HookCallback` | type | sessions.py: client_options | reference | none |
| `claude_agent_sdk.types` | `HookContext` | type | sessions.py: ThreadSession._on_stop, ThreadSession._on_tool_done | reference | none |
| `claude_agent_sdk.types` | `HookInput` | type | sessions.py: ThreadSession._on_stop, ThreadSession._on_tool_done, ThreadSession._note_cwd | reference | none |
| `claude_agent_sdk.types` | `HookJSONOutput` | type | sessions.py: ThreadSession._on_stop, ThreadSession._on_tool_done | reference | none |
| `claude_agent_sdk.types` | `HookMatcher` | type | sessions.py: client_options | reference | none |
| `claude_agent_sdk.types` | `PermissionMode` | type | sessions.py: ClaudeClient.set_permission_mode | reference | none |
| `claude_agent_sdk.types` | `PermissionResult` | type | sessions.py: ThreadSession._can_use_tool; approvals.py: to_permission | reference | none |
| `claude_agent_sdk.types` | `PermissionResultAllow` | type | approvals.py: to_permission | reference | none |
| `claude_agent_sdk.types` | `PermissionResultDeny` | type | sessions.py: ThreadSession._can_use_tool; approvals.py: to_permission | reference | none |
| `claude_agent_sdk.types` | `RateLimitEvent` | type | sessions.py: ThreadSession._dispatch | reference | none |
| `claude_agent_sdk.types` | `ServerToolResultBlock` | type | render/renderer.py: TurnRenderer._block | package | none |
| `claude_agent_sdk.types` | `ServerToolUseBlock` | type | render/renderer.py: TurnRenderer._block | package | none |
| `claude_agent_sdk.types` | `SystemMessage` | type | sessions.py: ThreadSession._dispatch | reference | none |
| `claude_agent_sdk.types` | `TERMINAL_TASK_STATUSES` | type | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | package | none |
| `claude_agent_sdk.types` | `TaskNotificationMessage` | type | sessions.py: TASK_MESSAGES, ThreadSession._dispatch, ThreadSession._ended_line; render/renderer.py: TaskFrame, TurnRenderer.feed | reference | none |
| `claude_agent_sdk.types` | `TaskProgressMessage` | type | sessions.py: TASK_MESSAGES; render/renderer.py: TaskFrame, TurnRenderer.feed | reference | none |
| `claude_agent_sdk.types` | `TaskStartedMessage` | type | sessions.py: TASK_MESSAGES, ThreadSession._dispatch, ThreadSession._record_task; render/renderer.py: TaskFrame, TurnRenderer._task_started, TurnRenderer._open_line | reference | none |
| `claude_agent_sdk.types` | `TaskUpdatedMessage` | type | sessions.py: TASK_MESSAGES, ThreadSession._dispatch; render/renderer.py: TaskFrame, TurnRenderer.feed | package | none |
| `claude_agent_sdk.types` | `TextBlock` | type | render/renderer.py: _words | reference | none |
| `claude_agent_sdk.types` | `ToolPermissionContext` | type | sessions.py: ThreadSession._can_use_tool; approvals.py: approval_blocks | reference | none |
| `claude_agent_sdk.types` | `ToolResultBlock` | type | render/renderer.py: TurnRenderer.feed, TurnRenderer._block | reference | none |
| `claude_agent_sdk.types` | `ToolUseBlock` | type | render/renderer.py: TurnRenderer._block | reference | none |
| `claude_agent_sdk` | `list_sessions` | function | sessions.py: directory_sessions | reference | P6 |
| `claude_agent_sdk` | `project_key_for_directory` | function | __main__.py: _alive_sessions | package | none |
| `claude_agent_sdk._internal.sessions` | `_canonicalize_path` | function | resume.py: by_last_activity | package | none |
| `claude_agent_sdk._internal.sessions` | `_find_project_dir` | function | resume.py: by_last_activity | package | none |
| `ClaudeSDKClient` | `connect` | method | sessions.py: ThreadSession._connect; footer.py: UsageProbe.__call__ | reference | none |
| `ClaudeSDKClient` | `disconnect` | method | sessions.py: ThreadSession._disconnect, ThreadSession._read; footer.py: UsageProbe.close | reference | none |
| `ClaudeSDKClient` | `get_context_usage` | method | sessions.py: ThreadSession._footer_data | reference | P2 |
| `ClaudeSDKClient` | `get_server_info` | method | sessions.py: ThreadSession.ensure_connected | reference | P2 |
| `ClaudeSDKClient` | `interrupt` | method | sessions.py: ThreadSession.stop | reference | P8 |
| `ClaudeSDKClient` | `query` | method | sessions.py: ThreadSession._work; footer.py: UsageProbe.__call__ | reference | none |
| `ClaudeSDKClient` | `receive_messages` | method | sessions.py: ThreadSession._read; footer.py: UsageProbe.__call__ | reference | none |
| `ClaudeSDKClient` | `set_model` | method | sessions.py: ThreadSession.apply_setup | reference | P18 |
| `ClaudeSDKClient` | `set_permission_mode` | method | sessions.py: ThreadSession.ensure_connected, ThreadSession.set_bypass | reference | P9 |
| `ClaudeSDKClient` | `stop_task` | method | sessions.py: ThreadSession.stop | reference | P12 |
| `ClaudeAgentOptions` | `can_use_tool` | option | sessions.py: client_options | reference | P11 |
| `ClaudeAgentOptions` | `cwd` | option | sessions.py: client_options; footer.py: UsageProbe.__init__ | reference | none |
| `ClaudeAgentOptions` | `effort` | option | sessions.py: client_options | reference | P16 |
| `ClaudeAgentOptions` | `extra_args` | option | sessions.py: client_options | reference | none |
| `ClaudeAgentOptions` | `hooks` | option | sessions.py: client_options | reference | none |
| `ClaudeAgentOptions` | `include_partial_messages` | option | sessions.py: client_options | reference | none |
| `ClaudeAgentOptions` | `resume` | option | sessions.py: client_options | reference | P7 |
| `ClaudeAgentOptions` | `setting_sources` | option | sessions.py: client_options; footer.py: UsageProbe.__init__ | reference | none |
| `ClaudeAgentOptions` | `stderr` | option | sessions.py: client_options | reference | none |
| `HookMatcher` | `hooks` | option | sessions.py: client_options | reference | none |
| `PermissionResultAllow` | `updated_input` | option | approvals.py: to_permission | reference | none |
| `PermissionResultDeny` | `message` | option | sessions.py: ThreadSession._can_use_tool; approvals.py: to_permission | reference | none |
| `list_sessions` | `directory` | option | sessions.py: directory_sessions | reference | none |
| `list_sessions` | `include_worktrees` | option | sessions.py: directory_sessions | reference | none |
| `AssistantMessage` | `content` | field | render/renderer.py: TurnRenderer._assistant | reference | none |
| `AssistantMessage` | `error` | field | render/renderer.py: TurnRenderer._assistant; sessions.py: ThreadSession._dispatch | reference | none |
| `AssistantMessage` | `message_id` | field | render/renderer.py: TurnRenderer._unannounced | reference | P20 |
| `AssistantMessage` | `parent_tool_use_id` | field | render/renderer.py: TurnRenderer._assistant; sessions.py: ThreadSession._dispatch | reference | none |
| `BaseHookInput` | `cwd` | field | sessions.py: ThreadSession._note_cwd | reference | P14 |
| `ContextUsageResponse` | `model` | field | sessions.py: ThreadSession._footer_data | reference | none |
| `ContextUsageResponse` | `percentage` | field | sessions.py: ThreadSession._footer_data | reference | none |
| `MessageOrigin` | `kind` | field | sessions.py: injected_turn | package | none |
| `ModelUsage` | `cacheCreationInputTokens` | field | footer.py: session_tokens | package | none |
| `ModelUsage` | `cacheReadInputTokens` | field | footer.py: session_tokens | package | none |
| `ModelUsage` | `inputTokens` | field | footer.py: session_tokens | package | none |
| `ModelUsage` | `outputTokens` | field | footer.py: session_tokens | package | none |
| `ResultMessage` | `model_usage` | field | footer.py: session_tokens | reference | none |
| `ResultMessage` | `origin` | field | sessions.py: injected_turn | reference | none |
| `ResultMessage` | `result` | field | render/renderer.py: TurnRenderer.feed; sessions.py: ThreadSession._finish; footer.py: UsageProbe.__call__ | reference | none |
| `ResultMessage` | `session_id` | field | sessions.py: ThreadSession._finish | reference | P3 |
| `ResultMessage` | `terminal_reason` | field | render/renderer.py: TurnRenderer.close; sessions.py: ThreadSession._finish | reference | none |
| `SDKSessionInfo` | `custom_title` | field | resume.py: matching | reference | none |
| `SDKSessionInfo` | `file_size` | field | resume.py: _row | reference | none |
| `SDKSessionInfo` | `git_branch` | field | resume.py: _row | reference | none |
| `SDKSessionInfo` | `last_modified` | field | resume.py: by_last_activity, _row; slack_app.py: channel_status_row | reference | none |
| `SDKSessionInfo` | `session_id` | field | resume.py: matching, by_last_activity, _row; __main__.py: _alive_sessions; home.py: Home._titles; slack_app.py: restart_waits, handle_resume, resume_into_thread, resume_clicked, show_resumed, channel_status | reference | none |
| `SDKSessionInfo` | `summary` | field | resume.py: _row; home.py: Home._titles; slack_app.py: restart_waits, resume_into_thread, show_resumed, channel_status_row | reference | none |
| `ServerToolResultBlock` | `content` | field | render/renderer.py: TurnRenderer._block, result_summary | package | none |
| `ServerToolResultBlock` | `tool_use_id` | field | render/renderer.py: TurnRenderer._block | package | none |
| `ServerToolUseBlock` | `id` | field | render/renderer.py: TurnRenderer._block | package | none |
| `ServerToolUseBlock` | `input` | field | render/renderer.py: TurnRenderer._block | package | none |
| `ServerToolUseBlock` | `name` | field | render/renderer.py: TurnRenderer._block | package | none |
| `StopHookInput` | `effort` | field | sessions.py: ThreadSession._on_stop | measured | none |
| `StreamEvent` | `event` | field | render/renderer.py: TurnRenderer.feed | reference | none |
| `StreamEvent` | `parent_tool_use_id` | field | render/renderer.py: TurnRenderer.feed | reference | none |
| `SystemMessage` | `data` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | reference | none |
| `SystemMessage` | `subtype` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskNotificationMessage` | `status` | field | sessions.py: ThreadSession._ended_line; render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskNotificationMessage` | `summary` | field | sessions.py: ThreadSession._ended_line; render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskNotificationMessage` | `tool_use_id` | field | sessions.py: ThreadSession._dispatch | reference | none |
| `TaskNotificationMessage` | `task_id` | field | sessions.py: ThreadSession._dispatch, ThreadSession._ended_line; render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskNotificationMessage` | `usage` | field | sessions.py: ThreadSession._ended_line | reference | none |
| `TaskProgressMessage` | `description` | field | render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskProgressMessage` | `task_id` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | reference | none |
| `TaskProgressMessage` | `tool_use_id` | field | sessions.py: ThreadSession._dispatch | reference | none |
| `TaskStartedMessage` | `description` | field | sessions.py: ThreadSession._record_task; render/renderer.py: TurnRenderer._task_started, TurnRenderer._open_line | reference | none |
| `TaskStartedMessage` | `task_id` | field | sessions.py: ThreadSession._dispatch, ThreadSession._record_task; render/renderer.py: TurnRenderer._task_started, TurnRenderer._open_line | reference | none |
| `TaskStartedMessage` | `task_type` | field | sessions.py: ThreadSession._record_task; render/renderer.py: TurnRenderer._open_line | reference | none |
| `TaskStartedMessage` | `tool_use_id` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.nests, TurnRenderer._task_started, TurnRenderer._open_line, TurnRenderer._outlived | reference | none |
| `TaskUpdatedMessage` | `status` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | package | none |
| `TaskUpdatedMessage` | `task_id` | field | sessions.py: ThreadSession._dispatch; render/renderer.py: TurnRenderer.feed | package | none |
| `TaskUsage` | `duration_ms` | field | sessions.py: ThreadSession._ended_line | reference | none |
| `TextBlock` | `text` | field | render/renderer.py: _words | reference | none |
| `ToolPermissionContext` | `description` | field | approvals.py: approval_blocks | reference | none |
| `ToolPermissionContext` | `title` | field | sessions.py: ThreadSession._can_use_tool; approvals.py: approval_blocks | reference | none |
| `ToolPermissionContext` | `tool_use_id` | field | sessions.py: ThreadSession._can_use_tool | reference | none |
| `ToolResultBlock` | `content` | field | render/renderer.py: TurnRenderer._block, result_summary | reference | none |
| `ToolResultBlock` | `is_error` | field | render/renderer.py: TurnRenderer._block | reference | none |
| `ToolResultBlock` | `tool_use_id` | field | render/renderer.py: TurnRenderer._block | reference | none |
| `ToolUseBlock` | `id` | field | render/renderer.py: TurnRenderer._block | reference | none |
| `ToolUseBlock` | `input` | field | render/renderer.py: TurnRenderer._block | reference | none |
| `ToolUseBlock` | `name` | field | render/renderer.py: TurnRenderer._block | reference | none |
| `UserMessage` | `content` | field | render/renderer.py: TurnRenderer.feed | reference | none |
| `UserMessage` | `parent_tool_use_id` | field | render/renderer.py: TurnRenderer.feed; sessions.py: ThreadSession._dispatch | reference | none |
| `UserMessage` | `tool_use_result` | field | render/renderer.py: TurnRenderer.feed | reference | P13 |
| `UserMessage` | `uuid` | field | sessions.py: ThreadSession._acknowledge | reference | P19 |
| `AskUserQuestion.input` | `questions` | key | sessions.py: ThreadSession._can_use_tool | measured | none |
| `AskUserQuestion.input.questions[]` | `header` | key | approvals.py: question_blocks, question_view | measured | none |
| `AskUserQuestion.input.questions[]` | `multiSelect` | key | approvals.py: _answer, question_view | measured | none |
| `AskUserQuestion.input.questions[]` | `options` | key | approvals.py: question_view, _answer, _says_more | measured | none |
| `AskUserQuestion.input.questions[]` | `question` | key | approvals.py: answered_blocks, draft_answers, question_view; render/previews.py: answered | measured | none |
| `AskUserQuestion.input.questions[].options[]` | `description` | key | approvals.py: _says_more, _option, _option_whole | measured | none |
| `AskUserQuestion.input.questions[].options[]` | `label` | key | approvals.py: _answer, _says_more, _option, _option_whole | measured | none |
| `ClaudeAgentOptions.extra_args` | `allow-dangerously-skip-permissions` | key | sessions.py: client_options | measured | none |
| `ClaudeAgentOptions.extra_args` | `replay-user-messages` | key | sessions.py: client_options | measured | P19 |
| `ClaudeSDKClient.get_server_info()` | `commands` | key | sessions.py: ThreadSession.ensure_connected | measured | none |
| `ClaudeSDKClient.get_server_info()` | `current_permission_mode` | key | sessions.py: ThreadSession.ensure_connected | measured | none |
| `ClaudeSDKClient.get_server_info()` | `models` | key | sessions.py: ThreadSession.ensure_connected | measured | P17 |
| `ClaudeSDKClient.get_server_info().commands[]` | `aliases` | key | commands.py: refused_in_thread | measured | none |
| `ClaudeSDKClient.get_server_info().commands[]` | `argumentHint` | key | commands.py: command_parts | measured | none |
| `ClaudeSDKClient.get_server_info().commands[]` | `description` | key | commands.py: command_parts | measured | none |
| `ClaudeSDKClient.get_server_info().commands[]` | `name` | key | commands.py: command_name; slack_app.py: submit_to_session | measured | none |
| `ClaudeSDKClient.get_server_info().models[]` | `description` | key | setup.py: _model_options | measured | none |
| `ClaudeSDKClient.get_server_info().models[]` | `displayName` | key | setup.py: _model_options, summary | measured | P17 |
| `ClaudeSDKClient.get_server_info().models[]` | `supportedEffortLevels` | key | setup.py: effort_levels | measured | P17 |
| `ClaudeSDKClient.get_server_info().models[]` | `supportsEffort` | key | setup.py: effort_levels | measured | none |
| `ClaudeSDKClient.get_server_info().models[]` | `value` | key | sessions.py: ThreadSession.ensure_connected; setup.py: effort_levels, _model_options, read_choice, summary | measured | P17 |
| `ClaudeSDKClient.query()` | `message` | key | prompt.py: user_message | measured | none |
| `ClaudeSDKClient.query()` | `parent_tool_use_id` | key | prompt.py: user_message | measured | none |
| `ClaudeSDKClient.query()` | `type` | key | prompt.py: user_message | measured | none |
| `ClaudeSDKClient.query()` | `uuid` | key | prompt.py: user_message | measured | P19 |
| `StopHookInput.effort` | `level` | key | sessions.py: ThreadSession._on_stop | measured | none |
| `StreamEvent.event` | `content_block` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event` | `delta` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event` | `message` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event` | `type` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.content_block` | `type` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.delta` | `text` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.delta` | `type` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.message` | `id` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `SystemMessage.data` | `claude_code_version` | key | sessions.py: ThreadSession._dispatch | measured | P1 |
| `SystemMessage.data` | `compact_metadata` | key | render/renderer.py: TurnRenderer.feed | measured | none |
| `SystemMessage.data` | `permissionMode` | key | sessions.py: ThreadSession._dispatch | measured | none |
| `SystemMessage.data` | `session_id` | key | sessions.py: ThreadSession._dispatch | measured | none |
| `SystemMessage.data.compact_metadata` | `post_tokens` | key | render/renderer.py: TurnRenderer._compacted | measured | none |
| `SystemMessage.data.compact_metadata` | `pre_tokens` | key | render/renderer.py: TurnRenderer._compacted | measured | none |
| `UserMessage.tool_use_result` | `content` | key | render/previews.py: preview | measured | none |
| `UserMessage.tool_use_result` | `filePath` | key | render/previews.py: preview | measured | none |
| `UserMessage.tool_use_result` | `structuredPatch` | key | render/previews.py: preview | measured | none |
| `UserMessage.tool_use_result` | `type` | key | render/previews.py: preview | measured | none |
| `UserMessage.tool_use_result.structuredPatch[]` | `lines` | key | render/previews.py: _diff | measured | none |
| `UserMessage.tool_use_result.structuredPatch[]` | `newStart` | key | render/previews.py: _diff | measured | none |
| `UserMessage.tool_use_result.structuredPatch[]` | `oldStart` | key | render/previews.py: _diff | measured | none |
| `AssistantMessageError` | `"authentication_failed"` | value | render/renderer.py: TurnRenderer._assistant | reference | none |
| `CanUseTool.tool_name` | `"AskUserQuestion"` | value | sessions.py: QUESTION_TOOL, ThreadSession._can_use_tool | measured | none |
| `HookEvent` | `"PostToolUse"` | value | sessions.py: client_options | reference | none |
| `HookEvent` | `"Stop"` | value | sessions.py: client_options | reference | none |
| `MessageOriginKind` | `"human"` | value | sessions.py: injected_turn | package | none |
| `PermissionMode` | `"auto"` | value | sessions.py: ThreadSession.auto | reference | none |
| `PermissionMode` | `"bypassPermissions"` | value | sessions.py: ThreadSession.ensure_connected, ThreadSession._bypass_runs, ThreadSession._mode_for, ThreadSession.status; sessions.py: ThreadSession.ensure_connected, ThreadSession._mode_for | reference | none |
| `PermissionMode` | `"default"` | value | sessions.py: ThreadSession.ensure_connected, ThreadSession._mode_for, ThreadSession.set_bypass | reference | none |
| `ResultMessage.terminal_reason` | `"aborted_streaming"` | value | render/renderer.py: INTERRUPTED, TurnRenderer.close; sessions.py: ThreadSession._finish | reference | none |
| `ResultMessage.terminal_reason` | `"aborted_tools"` | value | render/renderer.py: INTERRUPTED, TurnRenderer.close; sessions.py: ThreadSession._finish | reference | none |
| `ServerToolUseBlock.name` | `"Edit"` | value | render/previews.py: PREVIEWED; render/renderer.py: TurnRenderer._block | measured | none |
| `ServerToolUseBlock.name` | `"Write"` | value | render/previews.py: PREVIEWED; render/renderer.py: TurnRenderer._block | measured | none |
| `StreamEvent.event.content_block.type` | `"text"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.delta.type` | `"text_delta"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.type` | `"content_block_delta"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.type` | `"content_block_start"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `StreamEvent.event.type` | `"message_start"` | value | render/renderer.py: TurnRenderer.feed | measured | P20 |
| `StreamEvent.event.type` | `"message_stop"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `SystemMessage.subtype` | `"compact_boundary"` | value | render/renderer.py: TurnRenderer.feed | measured | none |
| `SystemMessage.subtype` | `"init"` | value | sessions.py: ThreadSession._dispatch | measured | P1 |
| `SystemMessage.subtype` | `"status"` | value | sessions.py: ThreadSession._dispatch | measured | none |
| `TaskNotificationMessage.status` | `"completed"` | value | sessions.py: ThreadSession._ended_line | reference | none |
| `TaskNotificationMessage.status` | `"failed"` | value | render/renderer.py: ended_line, terminal_status | reference | none |
| `TaskNotificationMessage.status` | `"killed"` | value | render/renderer.py: terminal_status | measured | none |
| `TaskNotificationMessage.status` | `"stopped"` | value | render/renderer.py: terminal_status | reference | none |
| `TaskStartedMessage.task_type` | `"local_agent"` | value | sessions.py: TASK_KINDS, ThreadSession._ended_line, ThreadSession._running_kinds | reference | none |
| `TaskStartedMessage.task_type` | `"local_bash"` | value | sessions.py: TASK_KINDS, SUMMARY_IS_END_LINE, ThreadSession._ended_line, ThreadSession._running_kinds | reference | none |
| `ToolUseBlock.name` | `"Bash"` | value | render/previews.py: WORDS, folded | measured | none |
| `ToolUseBlock.name` | `"Edit"` | value | render/previews.py: PREVIEWED, preview; render/renderer.py: TurnRenderer._block | measured | none |
| `ToolUseBlock.name` | `"Read"` | value | render/previews.py: WORDS, folded | measured | none |
| `ToolUseBlock.name` | `"Write"` | value | render/previews.py: PREVIEWED, preview; render/renderer.py: TurnRenderer._block | measured | none |
| `UserMessage.tool_use_result.type` | `"create"` | value | render/previews.py: preview | measured | none |

## Types the daemon does not read

The package exports these message, block and event types and the source imports none of them.
A message of a type `TurnRenderer.feed` does not match is left out of the reply. The probe
reports any other such type as new.

- `ConversationResetMessage`
- `HookEventMessage`
- `MirrorErrorMessage`
- `SessionMessage`
- `ThinkingBlock`

## Dependencies the table cannot hold

These are not names of the SDK, so no row can express them and no check covers them here.

- `footer.effort_change` reads the sentence Claude Code writes in `ResultMessage.result` after
  `/effort` and `/model`. The wording belongs to the CLI.
- `resume.py` and `__main__.py` read the CLI's transcript files on disk, in the folder that the
  private `_find_project_dir` returns.
- The rows under `AskUserQuestion.input` describe the input of a Claude Code tool, which the SDK
  hands to `can_use_tool` as a plain dict.
- The rows under `StreamEvent.event` are the Claude API's streaming events, which the SDK passes
  through unparsed. Their names are in the Claude API streaming reference.
- The two keys under `ClaudeAgentOptions.extra_args` are flags of the CLI that the SDK forwards.
