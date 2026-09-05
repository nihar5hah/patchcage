#!/usr/bin/env bash
# Self-check for scripts/install.sh (stubbed uv/node/npm/git/curl; no network).
# Exit non-zero if engine-only or engine+agent branches regress.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL="${ROOT}/scripts/install.sh"
FAIL=0

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "${got}" != "${want}" ]]; then
    printf 'FAIL %s: got %q want %q\n' "${label}" "${got}" "${want}" >&2
    FAIL=1
  else
    printf 'ok   %s\n' "${label}"
  fi
}

assert_file() {
  local label="$1" path="$2"
  if [[ ! -e "${path}" ]]; then
    printf 'FAIL %s: missing %s\n' "${label}" "${path}" >&2
    FAIL=1
  else
    printf 'ok   %s\n' "${label}"
  fi
}

run_case() {
  local name="$1" with_node="$2" onpath="${3:-}"
  local tmp stubs bin home src path_extra
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/pc-install-XXXXXX")"
  tmp="$(cd "${tmp}" && pwd)"
  stubs="${tmp}/stubs"
  bin="${tmp}/bin"
  home="${tmp}/home"
  src="${tmp}/src"
  mkdir -p "${stubs}" "${bin}" "${home}" "${src}/cli/packages/coding-agent"

  # Minimal package tree the installer accepts.
  printf 'name = "patchcage"\nversion = "0.0.0"\n' >"${src}/pyproject.toml"
  mkdir -p "${src}/cli"
  : >"${src}/cli/package.json"

  cat >"${stubs}/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# uv tool install --force <src>  (bin dir from UV_TOOL_BIN_DIR)
if [[ "${1:-}" == "tool" && "${2:-}" == "install" ]]; then
  dest="${UV_TOOL_BIN_DIR:?UV_TOOL_BIN_DIR unset}/patchcage-engine"
  printf '#!/bin/sh\necho stub-engine\n' >"${dest}"
  chmod +x "${dest}"
  exit 0
fi
exit 1
EOF
  chmod +x "${stubs}/uv"

  cat >"${stubs}/curl" <<'EOF'
#!/usr/bin/env bash
# Should not be called when uv is already on PATH.
echo "unexpected curl: $*" >&2
exit 1
EOF
  chmod +x "${stubs}/curl"

  cat >"${stubs}/git" <<'EOF'
#!/usr/bin/env bash
echo "unexpected git: $*" >&2
exit 1
EOF
  chmod +x "${stubs}/git"

  if [[ "${with_node}" == "1" || "${with_node}" == "old" ]]; then
    local node_ver="v22.19.0"
    [[ "${with_node}" == "old" ]] && node_ver="v18.0.0"
    cat >"${stubs}/node" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "-v" ]]; then echo "${node_ver}"; exit 0; fi
exit 0
EOF
    chmod +x "${stubs}/node"
  fi
  if [[ "${with_node}" == "1" ]]; then
    cat >"${stubs}/npm" <<EOF
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${src}/cli/packages/coding-agent/dist/bundle"
printf '#!/usr/bin/env node\nconsole.log("stub-agent")\n' \
  >"${src}/cli/packages/coding-agent/dist/bundle/cli.js"
chmod +x "${src}/cli/packages/coding-agent/dist/bundle/cli.js"
exit 0
EOF
    chmod +x "${stubs}/npm"
  fi

  path_extra=""
  [[ "${onpath}" == "onpath" ]] && path_extra=":${bin}"

  env -i \
    PATH="${stubs}:/bin:/usr/bin${path_extra}" \
    HOME="${home}" \
    PATCHCAGE_SRC="${src}" \
    PATCHCAGE_BIN="${bin}" \
    PATCHCAGE_HOME="${home}/.patchcage" \
    bash "${INSTALL}" >"${tmp}/out" 2>"${tmp}/err" || {
      printf 'FAIL %s: install exited %s\n' "${name}" "$?" >&2
      cat "${tmp}/err" >&2
      FAIL=1
      rm -rf "${tmp}"
      return
    }

  assert_file "${name}: engine bin" "${bin}/patchcage-engine"
  if [[ "${with_node}" == "1" ]]; then
    assert_file "${name}: agent link" "${bin}/patchcage"
    assert_eq "${name}: agent target" "$(readlink "${bin}/patchcage")" \
      "${src}/cli/packages/coding-agent/dist/bundle/cli.js"
  else
    if [[ -e "${bin}/patchcage" ]]; then
      printf 'FAIL %s: agent should be skipped\n' "${name}" >&2
      FAIL=1
    else
      printf 'ok   %s: agent skipped\n' "${name}"
    fi
  fi
  if [[ "${onpath}" == "onpath" ]]; then
    if grep -q 'Add to PATH' "${tmp}/out"; then
      printf 'FAIL %s: PATH hint should be omitted when bin dir is on PATH\n' "${name}" >&2
      FAIL=1
    else
      printf 'ok   %s: no PATH hint\n' "${name}"
    fi
  else
    if grep -q "Add to PATH" "${tmp}/out"; then
      printf 'ok   %s: PATH hint\n' "${name}"
    else
      printf 'FAIL %s: expected PATH hint\n' "${name}" >&2
      cat "${tmp}/out" >&2
      FAIL=1
    fi
  fi
  rm -rf "${tmp}"
}

[[ -f "${INSTALL}" ]] || { echo "missing ${INSTALL}" >&2; exit 1; }

run_case "engine-only" 0
run_case "engine+agent" 1
run_case "old-node" old
run_case "bin-on-path" 0 onpath

# Piped install must preserve an existing non-Git source directory.
preserve_tmp="$(mktemp -d "${TMPDIR:-/tmp}/pc-install-preserve-XXXXXX")"
mkdir -p "${preserve_tmp}/cache/src"
printf 'keep me\n' >"${preserve_tmp}/cache/src/keep.txt"
if env -i PATH="/bin:/usr/bin" HOME="${preserve_tmp}" \
  PATCHCAGE_HOME="${preserve_tmp}/cache" PATCHCAGE_BIN="${preserve_tmp}/bin" \
  bash <"${INSTALL}" >"${preserve_tmp}/out" 2>"${preserve_tmp}/err"; then
  echo 'FAIL existing non-Git source: install should refuse' >&2
  FAIL=1
fi
assert_file 'existing non-Git source preserved' "${preserve_tmp}/cache/src/keep.txt"
if ! grep -q 'refusing to replace' "${preserve_tmp}/err"; then
  echo 'FAIL existing non-Git source: wrong failure' >&2
  FAIL=1
fi
rm -rf "${preserve_tmp}"

if [[ "${FAIL}" -ne 0 ]]; then
  echo "test_install.sh: FAILED" >&2
  exit 1
fi
echo "test_install.sh: ok"
