# Attribution

`llm-obsidian` is an independent project that stands on the shoulders of the patterns, tools and authors listed below.

---

## Upstream — claude-obsidian

**Author:** AgriciDaniel
**License:** MIT (see [LICENSE](LICENSE))
**Repository:** https://github.com/AgriciDaniel/claude-obsidian

`llm-obsidian` began as a heavily reworked descendant of `claude-obsidian`: the wiki-companion skill set, the DragonScale Memory mechanisms and the vault layout originate there. The LICENSE file keeps the original copyright notice unchanged. Since then the retrieval stack, hooks, write-path and MCP integration have been redesigned (see the "Why this fork" section in the README for the full delta).

Lineage: `AgriciDaniel/claude-obsidian` (upstream) → private DevOps fork (mechanics incubator, 2026) → `llm-obsidian` (this repo, generic public release).

---

## Clarify workflow

**Author:** Matt Pocock
**Source:** https://github.com/mattpocock/skills/tree/main/skills/productivity/grilling
**License:** MIT

The `/clarify` workflow adapts the one-question-at-a-time interview pattern
from Matt Pocock's `grilling` skill and adds an explicit planning and
implementation alignment gate for llm-obsidian.

---

## LLM Wiki Pattern

**Author:** Andrej Karpathy
**Source:** https://github.com/karpathy
**Usage:** The core architecture — using an LLM to build and maintain a structured wiki from raw sources — follows the LLM Wiki pattern that Karpathy described publicly. This is an independent implementation; no code or content was copied from Karpathy's repositories.

---

## ITS CSS Snippets

**Author:** SlRvb
**Source:** https://github.com/SlRvb/Obsidian--ITS-Theme
**License:** GPL-2.0
**Files:**
- `.obsidian/snippets/ITS-Dataview-Cards.css`
- `.obsidian/snippets/ITS-Image-Adjustments.css`

These snippets are distributed under GPL-2.0. Any modification of these files must also be released under GPL-2.0.

---

## Obsidian community plugins (pre-installed)

The following Obsidian community plugins ship with this vault as pre-installed binaries. They belong to their authors and are redistributed here only to reduce installation friction. Check each plugin's repository for its license terms.

| Plugin | Author | Repository |
|--------|--------|-----------|
| Calendar | Liam Cain | https://github.com/liamcain/obsidian-calendar-plugin |
| Thino | Boninall (Quorafind) | https://github.com/Quorafind/Obsidian-Thino |
| Obsidian Excalidraw | Zsolt Viczian | https://github.com/zsviczian/obsidian-excalidraw-plugin |
| Obsidian Banners | Danny Hernandez | https://github.com/noatpad/obsidian-banners |

`obsidian-excalidraw-plugin/main.js` is **not** included in this repository; `bin/setup-vault.sh` downloads it from the plugin's official GitHub releases.

---

## Third-party runtime dependencies (not bundled)

| Tool | Author | Role |
|------|--------|------|
| [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy) | TBXark | The MCP HTTP gateway binary that `scripts/mcp-gateway/` manages |
| [ollama](https://ollama.com) + [BAAI bge-m3](https://huggingface.co/BAAI/bge-m3) | ollama / BAAI | Local embeddings for dense retrieval and semantic tiling |
| [context7](https://context7.com) | Upstash | Flagship example MCP server (hosted, library docs) |

**License of this repository:** MIT (upstream copyright notice preserved).
