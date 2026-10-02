<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

## 既存ファイルへの影響を抑える改修方針

- 改修では、既存ファイル・既存機能への影響をできるだけ小さくする。
- 新しい機能は可能な限り新しいファイルに分離し、既存ファイルの変更は読み込み・接続に必要な最小限にとどめる。
- 既存の共通処理を再利用し、依頼に不要なリファクタリング・整形・名前変更は行わない。
- 既存APIの認証・レスポンス・画面の動作を維持し、必要な変更だけを検証する。
