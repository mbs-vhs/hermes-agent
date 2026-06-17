## Grok-Specific Guidance

### Plan Mode & Tool Discipline
This repo is frequently worked through the **grok-build-vscode** extension (or equivalent Grok Build CLI). These rules are mandatory:

- The `exit_plan_mode` tool result is currently unreliable — it almost always reports "approved". **Never trust the tool result.**
- After calling `exit_plan_mode`, **end your turn immediately**. The real decision arrives in the *next* human message as `[Plan approved]` / `[Plan rejected]` / `[Plan cancelled]` (optional comment = guidance) or anything else (treat as a normal message).
- Use `enter_plan_mode` proactively for ambitious, ambiguous, or high-impact tasks; use `ask_user_question` to get decisions instead of guessing.

### Tool Usage
- You have strong parallel tool execution — use it.
- For long-running terminal work, prefer background mode and follow up with `get_command_or_subagent_output`.
- Prefer the available Grok skills (`/design`, `/implement`, `/review`, `/panel`, `/best-of-n`, `/execute-plan`, etc.) for subagent-style work.
- Always run the verification gate for this repo (below) before claiming completion — paste actual output, never "it passed".

### Communication Style
- Be direct and substantive; no filler closers.
- Surface blocked or ambiguous items with options instead of silently choosing.
