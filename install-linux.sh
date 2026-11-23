#!/usr/bin/env bash
# Install the Claude provider launchers on Linux without root privileges.
set -eu

bin_dir="${HOME}/.local/bin"
node_root="${HOME}/.local/share/claude-launchers/node"
check_only=0
work=""

usage() {
    cat <<'EOF'
Usage: bash install-linux.sh [--check-only] [--bin-dir DIR]

  --check-only   verify dependencies without installing anything
  --bin-dir DIR  where to create the launcher wrappers (default: ~/.local/bin)
EOF
}

say() { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$work" ] && [ -d "$work" ]; then
        rm -rf -- "$work"
    fi
}
new_work() {
    cleanup
    work=$(mktemp -d)
}
trap cleanup EXIT

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check-only) check_only=1 ;;
        --bin-dir) [ "$#" -ge 2 ] || fail '--bin-dir needs a value'; bin_dir=$2; shift ;;
        --bin-dir=*) bin_dir=${1#--bin-dir=} ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "unknown argument: $1" ;;
    esac
    shift
done

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for file in launch.py install-unix.py gpt-launch.mjs package.json; do
    [ -f "$repo_dir/$file" ] || fail "missing $file in $repo_dir; run the installer from the repository"
done

case "$(uname -m)" in
    x86_64|amd64) node_arch=linux-x64 ;;
    aarch64|arm64) node_arch=linux-arm64 ;;
    *) fail "unsupported architecture: $(uname -m)" ;;
esac

find_python() {
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

node_compatible() {
    "$1" -e 'const [a,b,c]=process.versions.node.split(".").map(Number);process.exit(a>20||(a===20&&(b>18||(b===18&&c>=1)))?0:1)' >/dev/null 2>&1
}

find_node() {
    if command -v node >/dev/null 2>&1 && node_compatible "$(command -v node)"; then
        command -v node
        return 0
    fi
    if [ -d "$node_root" ]; then
        for candidate in "$node_root"/*/bin/node; do
            if [ -x "$candidate" ] && node_compatible "$candidate"; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    fi
    return 1
}

find_claude() {
    if command -v claude >/dev/null 2>&1; then
        command -v claude
        return 0
    fi
    if [ -x "$bin_dir/claude" ]; then
        printf '%s\n' "$bin_dir/claude"
        return 0
    fi
    if [ -x "$HOME/.local/bin/claude" ]; then
        printf '%s\n' "$HOME/.local/bin/claude"
        return 0
    fi
    return 1
}

fetch() {
    url=$1
    destination=$2
    say "Downloading $url"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$destination" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$destination" "$url"
    else
        fail 'curl or wget is required to download the installers'
    fi
}

resolve_node_lts() {
    "$1" - <<'PY'
import json
import urllib.request

with urllib.request.urlopen('https://nodejs.org/dist/index.json', timeout=60) as response:
    releases = json.load(response)
minimum = (20, 18, 1)
for entry in releases:
    version = str(entry.get('version', ''))
    if not entry.get('lts') or not version.startswith('v'):
        continue
    try:
        parts = tuple(int(part) for part in version[1:].split('.'))
    except ValueError:
        continue
    if len(parts) == 3 and parts >= minimum:
        print(version)
        break
PY
}

install_node() {
    python=$1
    if ! version=$(resolve_node_lts "$python"); then
        fail 'could not read the Node.js release list from nodejs.org'
    fi
    [ -n "$version" ] || fail 'no Node.js LTS release >= 20.18.1 was found on nodejs.org'
    say "Installing Node.js $version into $node_root"
    new_work
    base="https://nodejs.org/dist/$version"
    tarball="node-$version-$node_arch.tar.xz"
    fetch "$base/SHASUMS256.txt" "$work/SHASUMS256.txt"
    fetch "$base/$tarball" "$work/$tarball"
    ( cd "$work" && grep " $tarball\$" SHASUMS256.txt | sha256sum -c - ) ||
        fail 'the Node.js download does not match the official checksum'
    tar -xJf "$work/$tarball" -C "$work" ||
        fail 'could not extract the Node.js archive; install xz-utils and retry'
    mkdir -p "$node_root"
    rm -rf -- "$node_root/$version"
    mv "$work/node-$version-$node_arch" "$node_root/$version"
    mkdir -p "$bin_dir"
    for executable in node npm npx corepack; do
        if [ -e "$node_root/$version/bin/$executable" ]; then
            ln -sfn "$node_root/$version/bin/$executable" "$bin_dir/$executable"
        fi
    done
    PATH="$bin_dir:$PATH"
    export PATH
}

install_claude() {
    new_work
    fetch 'https://claude.ai/install.sh' "$work/install.sh"
    if grep -qE '<!DOCTYPE|<html' "$work/install.sh" || ! head -n 1 "$work/install.sh" | grep -q '^#!'; then
        fail 'the Claude Code installer download is not a shell script'
    fi
    bash "$work/install.sh"
}

python_hint() {
    say 'Install Python 3.10 or newer with your package manager, for example:'
    say '  Debian/Ubuntu: sudo apt-get install -y python3'
    say '  Fedora/RHEL:   sudo dnf install -y python3'
    say '  Arch:          sudo pacman -S --needed python'
    say '  openSUSE:      sudo zypper install -y python3'
}

problems=0
if ! python=$(find_python); then
    say 'Python 3.10+ was not found.'
    python_hint
    problems=1
fi
if ! node=$(find_node); then
    say 'Node.js >=20.18.1 with npm was not found; the installer will download it when run.'
    if [ "$check_only" -eq 1 ]; then problems=1; fi
fi
if ! claude=$(find_claude); then
    say 'Claude Code was not found; the installer will download it when run.'
    if [ "$check_only" -eq 1 ]; then problems=1; fi
fi
if ! command -v git >/dev/null 2>&1; then
    say 'Note: git is not installed. You need it only to clone or update the repository.'
fi

if [ "$check_only" -eq 1 ]; then
    [ "$problems" -eq 0 ] || exit 1
    say 'All checks passed. Nothing was installed and PATH was not changed.'
    exit 0
fi

[ -n "$python" ] || exit 1

# The wrappers call python3 by name; make sure that name resolves.
if ! command -v python3 >/dev/null 2>&1; then
    mkdir -p "$bin_dir"
    ln -sfn "$python" "$bin_dir/python3"
fi

if [ -z "$node" ]; then
    say 'Node.js >=20.18.1 was not found.'
    install_node "$python"
    node=$(find_node) || fail 'Node.js was installed but no compatible binary is available'
fi
mkdir -p "$bin_dir"
case "$node" in
    "$bin_dir"/*) ;;
    *) ln -sfn "$node" "$bin_dir/node" ;;
esac
PATH="$bin_dir:$HOME/.local/bin:$PATH"
export PATH

if [ -z "$claude" ]; then
    say 'Claude Code was not found.'
    install_claude
    claude=$(find_claude) || fail 'the Claude Code installer finished but claude was not found'
fi

if [ ! -e "$bin_dir/claude" ] && [ "$claude" != "$bin_dir/claude" ]; then
    mkdir -p "$bin_dir"
    ln -sfn "$claude" "$bin_dir/claude"
fi

if ! ( cd "$repo_dir" && node --input-type=module -e "import('undici')" >/dev/null 2>&1 ); then
    command -v npm >/dev/null 2>&1 || fail 'npm is required for gpt_claude but was not found'
    say 'Installing locked npm dependencies...'
    ( cd "$repo_dir" && npm ci --ignore-scripts --no-audit ) ||
        fail 'npm ci failed; check network access and npm permissions'
fi

"$python" "$repo_dir/install-unix.py" --bin-dir "$bin_dir" ||
    fail 'could not create the launcher wrappers'

cleanup
work=$(mktemp -d)
for name in gpt_claude ai_claude ds_claude or_claude; do
    [ -x "$bin_dir/$name" ] || fail "missing launcher: $bin_dir/$name"
    if ! ( cd "$work" && "$bin_dir/$name" --version >/dev/null 2>&1 ); then
        fail "$bin_dir/$name --version failed"
    fi
done
rm -rf -- "$work"
work=""

say ''
say 'Launchers installed. Verify and configure providers:'
say "  $bin_dir/ds_claude --setkey"
say "  $bin_dir/or_claude --setkey"
say 'Then run ds_claude, or_claude, ai_claude or gpt_claude from your project.'
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say ''
       say 'Add this line to your shell profile to keep the commands in PATH:'
       say "  export PATH=\"$bin_dir:\$PATH\"" ;;
esac
