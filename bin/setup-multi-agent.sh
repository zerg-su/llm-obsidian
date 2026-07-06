#!/usr/bin/env bash
# llm-obsidian: multi-agent skill installer
# Wires the skills directory into each AI agent's expected location.
# Idempotent: safe to run multiple times.
#
# Supported agents:
#   - Claude Code    : auto-discovered via .claude-plugin/ (no symlink needed)
#   - Codex CLI      : repo Codex plugin marketplace + MCP mirror
#   - OpenCode       : symlink to ~/.opencode/skills/llm-obsidian
#   - Gemini CLI     : symlink to ~/.gemini/skills/llm-obsidian
#   - Cursor         : symlink to .cursor/skills (in repo)
#   - Windsurf       : symlink to .windsurf/skills (in repo)
#
# Bootstrap files (AGENTS.md, GEMINI.md, .cursor/rules/, .windsurf/rules/,
# .github/copilot-instructions.md) are already committed in the repo.
# This script wires up Codex-native packaging and legacy skill symlinks for
# agents that do not consume the repo plugin marketplace.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DIR="$REPO_ROOT/skills"
CODEX_MARKETPLACE="llm-obsidian-codex"

if [ ! -d "$SKILLS_DIR" ]; then
  echo "ERROR: $SKILLS_DIR does not exist. Are you running this from the llm-obsidian repo?"
  exit 1
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
GRAY='\033[0;37m'
NC='\033[0m'

link_if_missing() {
  local target="$1"
  local dest="$2"
  local agent_name="$3"

  mkdir -p "$(dirname "$dest")"

  if [ -L "$dest" ]; then
    local existing="$(readlink "$dest")"
    if [ "$existing" = "$target" ]; then
      echo -e "${GRAY}[$agent_name] already linked: $dest${NC}"
      return
    else
      echo -e "${YELLOW}[$agent_name] symlink exists but points elsewhere: $dest -> $existing (skipping, remove manually if you want to relink)${NC}"
      return
    fi
  fi

  if [ -e "$dest" ]; then
    echo -e "${YELLOW}[$agent_name] path exists and is not a symlink: $dest (skipping)${NC}"
    return
  fi

  ln -s "$target" "$dest"
  echo -e "${GREEN}[$agent_name] linked: $dest -> $target${NC}"
}

echo "llm-obsidian: multi-agent skill installer"
echo "Repo: $REPO_ROOT"
echo

# Codex CLI: plugin marketplace is canonical. A historical symlink can cause
# duplicate skills, so leave it untouched but make the state visible.
echo "Codex CLI"
if [ -x "$REPO_ROOT/scripts/codex-adapter.py" ]; then
  python3 "$REPO_ROOT/scripts/codex-adapter.py" --apply
fi
if [ -x "$REPO_ROOT/scripts/mcp-gateway/mcp-gateway.sh" ]; then
  "$REPO_ROOT/scripts/mcp-gateway/mcp-gateway.sh" codex-sync --apply || \
    echo -e "${YELLOW}[Codex CLI] MCP sync failed; run scripts/mcp-gateway/mcp-gateway.sh codex-sync --check for details.${NC}"
fi
LEGACY_CODEX_LINK="$HOME/.codex/skills/llm-obsidian"
if [ -L "$LEGACY_CODEX_LINK" ] && [ "$(readlink "$LEGACY_CODEX_LINK")" = "$SKILLS_DIR" ]; then
  echo -e "${YELLOW}[Codex CLI] legacy skill symlink still exists: $LEGACY_CODEX_LINK. Plugin install is canonical; remove the symlink manually if duplicate skills appear.${NC}"
fi
if command -v codex >/dev/null 2>&1 && [ "${CODEX_SKIP_PLUGIN_INSTALL:-0}" != "1" ]; then
  codex plugin marketplace add "$REPO_ROOT" >/dev/null 2>&1 || true
  if codex plugin add "llm-obsidian@$CODEX_MARKETPLACE" >/dev/null 2>&1; then
    echo -e "${GREEN}[Codex CLI] installed llm-obsidian@$CODEX_MARKETPLACE${NC}"
  else
    echo -e "${YELLOW}[Codex CLI] could not install llm-obsidian@$CODEX_MARKETPLACE; inspect with: codex plugin list${NC}"
  fi
elif ! command -v codex >/dev/null 2>&1; then
  echo -e "${YELLOW}[Codex CLI] codex command not found; generated repo files only.${NC}"
else
  echo -e "${GRAY}[Codex CLI] plugin install skipped by CODEX_SKIP_PLUGIN_INSTALL=1${NC}"
fi
echo

# OpenCode
link_if_missing "$SKILLS_DIR" "$HOME/.opencode/skills/llm-obsidian" "OpenCode"

# Gemini CLI
link_if_missing "$SKILLS_DIR" "$HOME/.gemini/skills/llm-obsidian" "Gemini CLI"

# Cursor (workspace-local)
link_if_missing "$SKILLS_DIR" "$REPO_ROOT/.cursor/skills" "Cursor"

# Windsurf (workspace-local)
link_if_missing "$SKILLS_DIR" "$REPO_ROOT/.windsurf/skills" "Windsurf"

echo
echo -e "${GREEN}Done.${NC} Bootstrap files (AGENTS.md, GEMINI.md, .cursor/rules/, .windsurf/rules/, .github/copilot-instructions.md) are already in this repo."
echo
echo "To verify each agent picks up the skills:"
echo "  - Claude Code: open the project, type /wiki"
echo "  - Codex CLI:   codex plugin list | grep llm-obsidian"
echo "                 start a new Codex thread after install; invoke explicitly as \$llm-obsidian:save"
echo "  - Cursor:      open the project, ask 'what skills do you have?'"
echo "  - Windsurf:    open in Cascade, ask the same"
echo "  - Gemini CLI:  gemini --list-skills (if supported)"
