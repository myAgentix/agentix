#!/usr/bin/env bash
# Agentix one-line installer — zero prior setup required.
#
# Installs the agentix kernel + the agentixd daemon (fastapi/uvicorn) + CLI + SDK
# into a self-contained venv, straight from the public git repo (no PyPI needed).
#
# Usage (subcommands: install | uninstall | upgrade; default is install).
# Pipe into BASH — not zsh/sh — e.g. `| bash -s -- install`:
#   curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | bash -s -- install
#   curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | bash -s -- upgrade
#   curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | bash -s -- uninstall
#
#   # add vendor/intrinsic extras onto the base daemon,cli,sdk set:
#   curl -LsSf https://raw.githubusercontent.com/myAgentix/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=openai-compat bash -s -- install
#   # pin an exact version / install a custom home:
#   curl -LsSf .../install.sh | AGENTIX_VERSION=v0.7.1 AGENTIX_HOME=~/myproject bash -s -- install
#
# Environment variables:
#   AGENTIX_HOME    — install root (default: ~/.agentix)
#   AGENTIX_EXTRAS  — comma-separated extras appended to daemon,cli,sdk
#                     (openai-compat,minio,hf,postgresql,broker,all)
#   AGENTIX_VERSION — pinned tag, e.g. v0.7.1 (default: latest semver tag)
#   AGENTIX_REF     — arbitrary git ref (branch/sha); overrides AGENTIX_VERSION
#   AGENTIX_REPO    — git remote to install from. Default is public HTTPS. For a PRIVATE
#                     repo use SSH (needs an SSH key that can reach it), e.g.
#                       AGENTIX_REPO=git@github.com:myAgentix/agentix.git
#                     Accepts scp-style (git@host:owner/repo.git) or a full ssh://…/https URL.
#   AGENTIX_PURGE   — uninstall only: 1 = also delete config.yaml + data (whole home)

set -euo pipefail

REPO_URL="${AGENTIX_REPO:-https://github.com/myAgentix/agentix.git}"
AGENTIX_HOME="${AGENTIX_HOME:-$HOME/.agentix}"
AGENTIX_EXTRAS="${AGENTIX_EXTRAS:-}"
AGENTIX_VERSION="${AGENTIX_VERSION:-}"
AGENTIX_REF="${AGENTIX_REF:-}"
AGENTIX_PURGE="${AGENTIX_PURGE:-}"
VENV="$AGENTIX_HOME/venv"

# Base extras that make agentixd (the daemon) actually runnable. User extras are
# appended so `AGENTIX_EXTRAS=openai-compat` adds the SDK without dropping the daemon.
BASE_EXTRAS="daemon,cli,sdk"

# ── parse subcommand + flags ─────────────────────────────────────────────────
CMD="install"
for arg in "$@"; do
    case "$arg" in
        install|uninstall|upgrade) CMD="$arg" ;;
        --purge) AGENTIX_PURGE=1 ;;
        *) echo "Agentix installer: unknown argument '$arg'" >&2; exit 1 ;;
    esac
done

# This script targets bash. It mostly works under zsh, but nudge users to the
# documented invocation so shell-compat is never a variable during a stalled run.
if [[ -z "${BASH_VERSION:-}" ]]; then
    echo "note: agentix installer is designed for bash — prefer '| bash -s -- $CMD'" >&2
fi

# Never let ANY git operation in this installer block on an interactive prompt — a
# credential request, or an unknown SSH host from a user's https->ssh `insteadOf`
# rewrite. A curl|bash install has no way to answer it, so fail fast instead. Applies
# to resolve_ref's `git ls-remote` and uv's clone alike.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

# ── OS check ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Linux|Darwin) ;;
    *) echo "Agentix installer: unsupported OS '$OS'. Use Linux or macOS." >&2; exit 1 ;;
esac

# ── Python 3.12+ ──────────────────────────────────────────────────────────────
ensure_python() {
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" &>/dev/null; then
            ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])')
            if [[ "$ver" > "(3, 11)" ]]; then
                PYTHON="$candidate"
                return
            fi
        fi
    done

    echo "Python 3.12+ not found — installing..."
    if [[ "$OS" == "Linux" ]]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y python3.12 python3.12-venv
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3.12
        else
            echo "Cannot auto-install Python on this Linux distro. Install Python 3.12+ manually." >&2
            exit 1
        fi
    elif [[ "$OS" == "Darwin" ]]; then
        if command -v brew &>/dev/null; then
            brew install python@3.12
        else
            echo "Homebrew not found. Install Python 3.12+ from https://python.org or install Homebrew first." >&2
            exit 1
        fi
    fi
    PYTHON=python3.12
}

# ── uv ────────────────────────────────────────────────────────────────────────
ensure_uv() {
    if ! command -v uv &>/dev/null; then
        echo "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    fi
}

# ── normalise REPO_URL into a pip/uv `git+…` URL ──────────────────────────────
# `git ls-remote` accepts scp-style (git@host:owner/repo.git), but pip/uv need a real
# URL: git+ssh://git@host/owner/repo.git. Convert the scp colon to a path slash.
pip_git_url() {
    local url="$REPO_URL"
    if [[ "$url" == git@* ]]; then
        url="ssh://${url/:/\/}"           # git@host:owner/repo.git -> ssh://git@host/owner/repo.git
    fi
    printf 'git+%s' "$url"
}

# ── resolve the git ref to install ────────────────────────────────────────────
# Precedence: AGENTIX_REF (any ref) > AGENTIX_VERSION (exact tag) > latest tag.
resolve_ref() {
    if [[ -n "$AGENTIX_REF" ]]; then
        REF="$AGENTIX_REF"
    elif [[ -n "$AGENTIX_VERSION" ]]; then
        REF="$AGENTIX_VERSION"
    else
        REF=$(git ls-remote --tags --refs "$REPO_URL" \
              | awk -F/ '{print $NF}' \
              | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
              | sort -V | tail -1)
        if [[ -z "$REF" ]]; then
            echo "Could not resolve a latest semver tag from $REPO_URL — set AGENTIX_VERSION." >&2
            exit 1
        fi
    fi
    EXTRAS="$BASE_EXTRAS"
    [[ -n "$AGENTIX_EXTRAS" ]] && EXTRAS="${BASE_EXTRAS},${AGENTIX_EXTRAS}"
    SPEC="agentix[${EXTRAS}] @ $(pip_git_url)@${REF}"
}

installed_version() {
    "$VENV/bin/python" -c "import agentix; print(agentix.__version__)" 2>/dev/null || echo "unknown"
}

# Install $SPEC from the git source. uv prints nothing while it resolves the dependency
# tree (30-90s on first run), so announce the wait — otherwise the install looks hung.
# git is already forced non-interactive globally (see top), so a bad credential/host
# errors fast rather than blocking. Extra args (e.g. --upgrade) go to `uv pip install`.
pip_install_spec() {
    echo "Resolving + installing from git ($REF) — first run can take 30-90s, please wait..."
    uv pip install --python "$VENV/bin/python" "$@" "$SPEC"
}

write_env_snippets() {
    ENV_SH="$AGENTIX_HOME/env.sh"
    cat > "$ENV_SH" <<EOF
# Source this to activate the Agentix environment:  source ~/.agentix/env.sh
export AGENTIX_HOME="$AGENTIX_HOME"
export PATH="$VENV/bin:\$PATH"
EOF
    FISH_DIR="$HOME/.config/fish/conf.d"
    if [[ -d "$FISH_DIR" ]]; then
        cat > "$FISH_DIR/agentix.fish" <<EOF
set -gx AGENTIX_HOME "$AGENTIX_HOME"
fish_add_path "$VENV/bin"
EOF
    fi
}

# ── stop a running daemon bound under this AGENTIX_HOME ───────────────────────
# Target the process holding the daemon's socket precisely via fuser — never a
# broad `pkill -f`, which substring-matches every command line and can kill
# unrelated processes (e.g. an editor open on that path).
stop_daemon() {
    SOCK="$AGENTIX_HOME/agentixd.sock"
    if [[ -S "$SOCK" ]] && command -v fuser &>/dev/null; then
        fuser -k "$SOCK" &>/dev/null || true
    fi
    [[ -S "$SOCK" ]] && rm -f "$SOCK" || true
}

# ── commands ──────────────────────────────────────────────────────────────────
do_install() {
    echo "Installing Agentix into $AGENTIX_HOME ..."
    mkdir -p "$AGENTIX_HOME"
    ensure_python
    ensure_uv
    resolve_ref

    uv venv "$VENV" --python "$PYTHON"
    echo "venv ready — installing packages..."
    pip_install_spec
    write_env_snippets

    echo ""
    echo "Agentix installed: $SPEC"
    echo "  agentix  version: $(installed_version)"
    echo ""
    echo "Activate in the current shell:"
    echo "  source $AGENTIX_HOME/env.sh"
    echo ""
    echo "Start the daemon (Unix socket at $AGENTIX_HOME/agentixd.sock):"
    echo "  $VENV/bin/agentixd"
    echo "Probe liveness:"
    echo "  curl --unix-socket $AGENTIX_HOME/agentixd.sock http://x/health/live"
    echo ""
    echo "Docs: https://github.com/myAgentix/agentix/blob/main/docs/quickstart.md"
}

do_upgrade() {
    if [[ ! -x "$VENV/bin/python" ]]; then
        echo "No existing install at $VENV — running a fresh install instead."
        do_install
        return
    fi
    ensure_uv
    resolve_ref
    OLD=$(installed_version)
    stop_daemon
    echo "Upgrading agentix in $VENV ..."
    pip_install_spec --upgrade
    write_env_snippets
    echo ""
    echo "Agentix upgraded: $OLD -> $(installed_version)  (ref: $REF)"
}

do_uninstall() {
    stop_daemon
    rm -rf "$VENV" "$AGENTIX_HOME/env.sh"
    rm -f "$HOME/.config/fish/conf.d/agentix.fish"
    if [[ "$AGENTIX_PURGE" == "1" ]]; then
        rm -rf "$AGENTIX_HOME"
        echo "Agentix fully removed (purged $AGENTIX_HOME, including config + data)."
    else
        echo "Agentix removed from $AGENTIX_HOME."
        echo "Kept: config.yaml and data (if any). Re-run with --purge (or AGENTIX_PURGE=1) to delete them."
    fi
}

case "$CMD" in
    install)   do_install ;;
    upgrade)   do_upgrade ;;
    uninstall) do_uninstall ;;
esac
