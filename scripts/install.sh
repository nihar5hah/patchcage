#!/usr/bin/env bash
# PatchCage installer: uv tool → patchcage-engine; optional Node agent → patchcage.
# Agent mode does not need Docker. Live /sandbox does.
#
#   curl -fsSL https://raw.githubusercontent.com/nihar5hah/patchcage/main/scripts/install.sh | bash
#   bash scripts/install.sh   # from a checkout: installs that tree
#
# Env overrides:
#   PATCHCAGE_REPO   git URL (default: https://github.com/nihar5hah/patchcage.git)
#   PATCHCAGE_REF    branch or tag (default: main; not a SHA — git clone --branch)
#   PATCHCAGE_SRC    use this source tree (skip clone)
#   PATCHCAGE_HOME   clone/cache root (default: ~/.patchcage)
#   PATCHCAGE_BIN    bin dir  (default: ~/.local/bin)
#   PATCHCAGE_SKIP_AGENT=1  engine only
set -euo pipefail

REPO="${PATCHCAGE_REPO:-https://github.com/nihar5hah/patchcage.git}"
REF="${PATCHCAGE_REF:-main}"
HOME_DIR="${PATCHCAGE_HOME:-$HOME/.patchcage}"
BIN_DIR="${PATCHCAGE_BIN:-$HOME/.local/bin}"
MIN_NODE_MAJOR=22
MIN_NODE_MINOR=19

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

path_contains_dir() {
  local needle="$1" p resolved
  case ":${PATH}:" in
    *":${needle}:"*) return 0 ;;
  esac
  local IFS=:
  for p in ${PATH}; do
    [[ -n "${p}" && -d "${p}" ]] || continue
    resolved="$(cd "${p}" && pwd)" || continue
    [[ "${resolved}" == "${needle}" ]] && return 0
  done
  return 1
}

resolve_src() {
  if [[ -n "${PATCHCAGE_SRC:-}" ]]; then
    SRC="$(cd "${PATCHCAGE_SRC}" && pwd)" || die "source directory does not exist"
    return
  fi
  local here root
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    root="$(cd "${here}/.." && pwd)"
    if [[ -f "${root}/pyproject.toml" ]] && grep -q 'name = "patchcage"' "${root}/pyproject.toml"; then
      SRC="${root}"
      return
    fi
  fi
  need_cmd git
  SRC="${HOME_DIR}/src"
  mkdir -p "${HOME_DIR}"
  if [[ -d "${SRC}/.git" ]]; then
    [[ -z "$(git -C "${SRC}" status --porcelain)" ]] \
      || die "refusing to update dirty source tree ${SRC}; commit or stash changes first"
    say "Updating ${SRC} (${REF})…"
    git -C "${SRC}" fetch --depth 1 origin "${REF}"
    git -C "${SRC}" checkout -q FETCH_HEAD \
      || die "could not update ${SRC} (dirty tree?); commit, stash, or remove it and re-run"
  else
    [[ ! -e "${SRC}" && ! -L "${SRC}" ]] \
      || die "refusing to replace existing non-Git directory ${SRC}"
    say "Cloning ${REPO} (${REF}) → ${SRC}…"
    git clone --depth 1 --branch "${REF}" "${REPO}" "${SRC}"
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  need_cmd curl
  say "Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Official installer puts the binary in ~/.local/bin (or UV_INSTALL_DIR).
  export PATH="${HOME}/.local/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; add ~/.local/bin and re-run"
}

install_engine() {
  say "Installing patchcage-engine via uv tool…"
  mkdir -p "${BIN_DIR}"
  # --reinstall-package: uv caches directory builds by pyproject mtime, so a
  # re-run after editing src/ would otherwise install stale code.
  UV_TOOL_BIN_DIR="${BIN_DIR}" uv tool install --force --reinstall-package patchcage \
    --constraints "${SRC}/requirements.lock" "${SRC}"
  [[ -x "${BIN_DIR}/patchcage-engine" ]] \
    || die "patchcage-engine not found after uv tool install (${BIN_DIR})"
  say "  → ${BIN_DIR}/patchcage-engine"
}

node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local ver major minor
  ver="$(node -v 2>/dev/null | sed 's/^v//')" || return 1
  [[ -n "${ver}" ]] || return 1
  major="${ver%%.*}"
  minor="${ver#*.}"
  minor="${minor%%.*}"
  [[ "${major}" =~ ^[0-9]+$ && "${minor}" =~ ^[0-9]+$ ]] || return 1
  [[ "${major}" -gt "${MIN_NODE_MAJOR}" ]] \
    || { [[ "${major}" -eq "${MIN_NODE_MAJOR}" ]] && [[ "${minor}" -ge "${MIN_NODE_MINOR}" ]]; }
}

install_agent() {
  if [[ "${PATCHCAGE_SKIP_AGENT:-}" == "1" ]]; then
    say "Skipping agent (PATCHCAGE_SKIP_AGENT=1)."
    return
  fi
  if ! node_ok; then
    say "Skipping agent: need Node ${MIN_NODE_MAJOR}.${MIN_NODE_MINOR}+ on PATH (engine-only install is fine)."
    say "  Agent mode does not need Docker; install Node and re-run this script to add \`patchcage\`."
    return
  fi
  need_cmd npm
  say "Building TypeScript agent (npm ci --ignore-scripts && npm run build)…"
  (
    cd "${SRC}/cli"
    npm ci --ignore-scripts
    npm run build
  )
  local cli_js="${SRC}/cli/packages/coding-agent/dist/bundle/cli.js"
  [[ -f "${cli_js}" ]] || die "agent build missing ${cli_js}"
  chmod +x "${cli_js}"
  mkdir -p "${BIN_DIR}"
  ln -sfn "${cli_js}" "${BIN_DIR}/patchcage"
  say "  → ${BIN_DIR}/patchcage"
}

print_next() {
  say ""
  say "Done."
  say "  patchcage-engine  — Python harness (Docker needed only for live /sandbox)"
  if [[ -x "${BIN_DIR}/patchcage" || -L "${BIN_DIR}/patchcage" ]]; then
    say "  patchcage         — unsandboxed agent TUI (/sandbox spawns the engine)"
    say "                      (symlink into ${SRC}; keep that tree)"
  fi
  if [[ "${PATH_HAD_BIN}" -eq 0 ]]; then
    say ""
    say "Add to PATH (zsh/bash):"
    say "  export PATH=\"${BIN_DIR}:\$PATH\""
  fi
  say ""
  say "Next: point a Completions model at Ollama/llama.cpp (see README), then \`patchcage\`."
  say "Docker + \`python scripts/build_runtime_image.py\` only when you run /sandbox."
}

main() {
  mkdir -p "${BIN_DIR}"
  BIN_DIR="$(cd "${BIN_DIR}" && pwd)"
  PATH_HAD_BIN=0
  if path_contains_dir "${BIN_DIR}"; then
    PATH_HAD_BIN=1
  fi

  resolve_src
  [[ -f "${SRC}/pyproject.toml" ]] || die "no pyproject.toml in ${SRC}"
  grep -q 'name = "patchcage"' "${SRC}/pyproject.toml" \
    || die "${SRC} is not a PatchCage tree (pyproject name)"
  ensure_uv
  # Lookups in this process should see bins we just wrote; PATH_HAD_BIN is the
  # caller's PATH, used only for the post-install hint.
  export PATH="${BIN_DIR}:${PATH}"
  install_engine
  install_agent
  print_next
}

main "$@"
