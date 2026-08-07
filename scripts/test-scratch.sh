#!/usr/bin/env bash
# Shared portable scratch allocator for repository-owned shell test suites.

llm_obsidian_test_scratch_dir() {
    local prefix="${1:-llm-obsidian-test}"
    local root
    case "$prefix" in
        *[!A-Za-z0-9._-]*|'')
            printf 'invalid test scratch prefix: %s\n' "$prefix" >&2
            return 2
            ;;
    esac
    if [ -n "${TMPDIR:-}" ]; then
        root="$TMPDIR"
    else
        root="$(python3 -c 'import tempfile; print(tempfile.gettempdir())')" || return
    fi
    mkdir -p "$root" || return
    mktemp -d "$root/$prefix.XXXXXX"
}
