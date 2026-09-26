#!/usr/bin/env bash
# Publish the open-source version of this repository, WITH its commit history.
#
#   scripts/export-oss.sh --check      build + scan the public history, push nothing
#   scripts/export-oss.sh --audit      same, plus a report of every host / email /
#                                      IP / secret-shaped string in the whole history
#   scripts/export-oss.sh --push       build + scan, then push to the public remote
#   scripts/export-oss.sh --worktree   quick denylist scan of the working tree only
#
# How it works. The internal repository is the source of truth. Each run clones
# its committed `main` and rewrites that history with git filter-repo:
#
#   .oss/exclude       paths removed from EVERY commit (commits left empty vanish)
#   .oss/replacements  text rewritten in every file version and commit message
#   .oss/mailmap       internal author identities -> the ones published
#
# then scans every file version, every commit message and every identity against
# .oss/denylist and refuses to publish anything that matches.
#
# The rewrite is deterministic: the same history and the same rules produce the
# same commit hashes. So each export's result begins with exactly the commits the
# public repo already has, plus the new ones — a plain fast-forward push. Two
# things break that, and the script refuses rather than force-pushing:
#
#   * A rule change that touches OLD commits (a new exclude path or replacement
#     matching something already published) changes their hashes. Prefer rules
#     that only match new work; when that is impossible, publishing needs
#     --force, which rewrites the public history for everyone.
#   * A commit made directly on the public repo (a pull request merged on GitHub).
#     Never merge on GitHub: apply the contribution here (`git am` keeps its
#     author), export, and close the pull request pointing at the synced commit.
#
# Remote: $OSS_REMOTE, else the first non-comment line of .oss/remote.
# Branch: $OSS_BRANCH, default main.
# Needs git-filter-repo (`pip install git-filter-repo`), or $OSS_FILTER_REPO.
set -euo pipefail

usage() {
  echo "usage: $0 --check | --audit | --push [--force] | --worktree" >&2
  exit 2
}
MODE=""; FORCE=""
for arg in "$@"; do
  case "$arg" in
    --check|--audit|--push|--worktree) [ -z "$MODE" ] || usage; MODE="${arg#--}" ;;
    --force) FORCE=1 ;;
    *) usage ;;
  esac
done
[ -n "$MODE" ] || usage
[ -z "$FORCE" ] || [ "$MODE" = push ] || usage

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
BRANCH="${OSS_BRANCH:-main}"
for f in exclude replacements mailmap denylist; do
  [ -f ".oss/$f" ] || { echo "❌ missing .oss/$f" >&2; exit 1; }
done
DENY_PATTERN="$(grep -v '^\s*#' .oss/denylist | grep -v '^\s*$' | paste -sd'|' -)"

# ── --worktree: the fast pre-commit check (tracked + untracked, not ignored) ──
if [ "$MODE" = worktree ]; then
  HITS="$(git ls-files -co --exclude-standard -z | python3 -c '
import fnmatch, os, re, sys
rules = [l.strip() for l in open(".oss/exclude") if l.strip() and not l.startswith("#")]
deny = re.compile(sys.argv[1], re.I)
for path in filter(None, sys.stdin.read().split("\0")):
    if any(fnmatch.fnmatch(path, r) for r in rules) or not os.path.isfile(path):
        continue
    try:
        text = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if deny.search(line):
            print(f"{path}:{n}: {line.strip()[:160]}")
' "$DENY_PATTERN")"
  if [ -n "$HITS" ]; then
    echo "❌ internal references in the working tree:" >&2
    echo "$HITS" >&2
    exit 1
  fi
  echo "✅ working tree clean (this checks current files only — --check scans the history)"
  exit 0
fi

# ── resolve tools and target ──
if [ -n "${OSS_FILTER_REPO:-}" ]; then FILTER_REPO=("$OSS_FILTER_REPO")
elif git filter-repo --version >/dev/null 2>&1; then FILTER_REPO=(git filter-repo)
elif command -v git-filter-repo >/dev/null 2>&1; then FILTER_REPO=(git-filter-repo)
else
  echo "❌ git-filter-repo not found: pip install git-filter-repo (or set OSS_FILTER_REPO)" >&2
  exit 1
fi
REMOTE="${OSS_REMOTE:-$(grep -v '^\s*#' .oss/remote 2>/dev/null | grep -v '^\s*$' | head -1 | tr -d '[:space:]' || true)}"
if [ "$MODE" = push ] && [ -z "$REMOTE" ]; then
  echo "❌ --push needs the public remote: put its URL in .oss/remote or set OSS_REMOTE" >&2
  exit 1
fi

SRC="$(git rev-parse --verify -q "refs/heads/$BRANCH")" || { echo "❌ no local branch $BRANCH" >&2; exit 1; }
if [ "$(git rev-parse HEAD)" != "$SRC" ] || [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  exporting committed '$BRANCH' ($(git rev-parse --short "$SRC")) — your checkout differs from it or has uncommitted changes, which are NOT included." >&2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. rewrite the history ──
git clone -q --no-local --single-branch --branch "$BRANCH" --no-tags "$ROOT" "$WORK/repo"
grep -v '^\s*#' .oss/exclude | grep -v '^\s*$' | sed 's/^/glob:/' > "$WORK/paths"
# filter-repo has no comment syntax: every line of a replacements file is a rule,
# and a bare "#" line would rewrite every "#" in the history. Strip ours first.
grep -v '^\s*#' .oss/replacements | grep -v '^\s*$' > "$WORK/replacements" || true
( cd "$WORK/repo" && "${FILTER_REPO[@]}" --force --quiet \
    --invert-paths --paths-from-file "$WORK/paths" \
    --replace-text "$WORK/replacements" \
    --replace-message "$WORK/replacements" \
    --mailmap "$ROOT/.oss/mailmap" ) >"$WORK/filter.log" 2>&1 || {
  echo "❌ git filter-repo failed:" >&2; cat "$WORK/filter.log" >&2; exit 1; }
git -C "$WORK/repo" remote remove origin 2>/dev/null || true
NEW="$(git -C "$WORK/repo" rev-parse "$BRANCH")"
echo "🧬 public history: $(git -C "$WORK/repo" rev-list --count "$BRANCH") commits (internal: $(git rev-list --count "$SRC")), head $(git -C "$WORK/repo" rev-parse --short "$NEW")"

# ── 2a. the published tip must be byte-identical to the internal tip ──
# Replacements exist to clean OLD history. The current code is expected to be
# clean at the source, so the public head must equal internal `main` minus the
# excluded paths, file for file. A difference means a rule is rewriting current
# content (a broken rule, or internal text that should be fixed in the code).
python3 - "$ROOT" "$SRC" "$WORK/repo" "$NEW" <<'PY'
import fnmatch, subprocess, sys
root, src, repo, new = sys.argv[1:5]
rules = [l.strip() for l in open(f"{root}/.oss/exclude") if l.strip() and not l.startswith("#")]
def tree(where, rev):
    out = subprocess.run(["git", "-C", where, "ls-tree", "-r", "-z", rev], capture_output=True, check=True).stdout
    entries = {}
    for rec in filter(None, out.split(b"\0")):
        meta, path = rec.decode().split("\t", 1)
        entries[path] = meta.split()[2]
    return entries
internal = {p: b for p, b in tree(root, src).items() if not any(fnmatch.fnmatch(p, r) for r in rules)}
public = tree(repo, new)
diff = sorted(p for p in set(internal) | set(public) if internal.get(p) != public.get(p))
if diff:
    print("❌ the public head differs from internal main in these files — a replacement rule is", file=sys.stderr)
    print("   rewriting current content. Fix the text in the code (or the rule):", file=sys.stderr)
    for p in diff[:50]:
        state = "missing in public" if p not in public else "extra in public" if p not in internal else "content changed"
        print(f"   {p}  ({state})", file=sys.stderr)
    sys.exit(1)
print(f"🟰 public head matches internal main ({len(public)} files)")
PY

# ── 2b. scan everything that would be published ──
python3 - "$WORK/repo" "$ROOT/.oss/denylist" "$ROOT/.oss/mailmap" "$MODE" <<'PY'
import re, subprocess, sys
from collections import defaultdict

repo, denyfile, mailmap, mode = sys.argv[1:5]
terms = [l.strip() for l in open(denyfile) if l.strip() and not l.lstrip().startswith("#")]
deny = re.compile("|".join(f"(?:{t})" for t in terms), re.I)
published = set(re.findall(r"^[^#<]*<([^>]+)>", open(mailmap).read(), re.M))

def git(*a, **kw):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True, **kw).stdout

# every blob reachable from the history, with a path and commit to report it by
where = {}
commit = ""
for line in git("log", "--all", "--format=@%h", "--raw", "--no-abbrev", "-m", "--no-renames").decode().splitlines():
    if line.startswith("@"):
        commit = line[1:]
    elif line.startswith(":"):
        meta, path = line.split("\t", 1)
        blob = meta.split()[3]
        if set(blob) != {"0"} and blob not in where:
            where[blob] = (path, commit)

hits, audit = [], defaultdict(set)
AUDIT = {
    "host": re.compile(r"\b(?:[a-z0-9-]+\.)+(?:net|com|ir|io|org|dev|local|internal|corp|lan)\b", re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "ipv4": re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b"),
    "secret": re.compile(r"(?i)(?:token|secret|password|passwd|api[_-]?key)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9+/_.\-]{20,}"),
}
blobs = list(where)
out = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="\n".join(blobs).encode(),
                     capture_output=True, check=True).stdout
pos = 0
for blob in blobs:
    eol = out.index(b"\n", pos)
    size = int(out[pos:eol].split()[2])
    data = out[eol + 1: eol + 1 + size]
    pos = eol + 2 + size
    if b"\0" in data[:8000]:
        continue  # binary
    path, c = where[blob]
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if deny.search(line):
            hits.append(f"{c} {path}:{n}: {line.strip()[:120]}")
        if mode == "audit":
            for kind, rx in AUDIT.items():
                for m in rx.finditer(line):
                    audit[(kind, m.group(0)[:70])].add(path)

bad_idents = set()
for rec in git("log", "--all", "--format=%h%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01").decode().split("\x01"):
    if not rec.strip():
        continue
    h, an, ae, cn, ce, body = rec.strip("\n").split("\x00", 5)
    for name, email in ((an, ae), (cn, ce)):
        if email not in published or deny.search(f"{name} <{email}>"):
            bad_idents.add(f"{name} <{email}>")
    for n, line in enumerate(body.splitlines(), 1):
        if deny.search(line):
            hits.append(f"{h} [commit message line {n}]: {line.strip()[:120]}")
        if mode == "audit":
            for kind in ("host", "email", "ipv4"):
                for m in AUDIT[kind].finditer(line):
                    audit[(kind, m.group(0))].add(f"<message {h}>")
    if mode == "audit":
        audit[("identity", f"{an} <{ae}>")].add("<author>")

print(f"🔎 scanned {len(blobs)} file versions and {len(git('rev-list', '--all').split())} commit messages")
if mode == "audit":
    print("📋 audit — every host/email/IP/secret-shaped string in the public history. Read it:")
    for (kind, val), paths in sorted(audit.items()):
        ps = sorted(paths)
        print(f"   {kind:9} {val:70.70} {len(ps):3}×  e.g. {ps[0]}")
failed = False
if bad_idents:
    failed = True
    print("❌ identities not mapped in .oss/mailmap (or matching the denylist):", file=sys.stderr)
    for i in sorted(bad_idents):
        print(f"   {i}", file=sys.stderr)
if hits:
    failed = True
    print(f"❌ {len(hits)} denylist hit(s) in the public history — add a line to .oss/replacements "
          f"(or a path to .oss/exclude):", file=sys.stderr)
    for h in sorted(set(hits))[:100]:
        print(f"   {h}", file=sys.stderr)
sys.exit(1 if failed else 0)
PY
echo "✅ history clean"

[ "$MODE" = push ] || { echo "(nothing pushed — use --push to publish)"; exit 0; }

# ── 3. publish: fast-forward only, unless --force ──
git -C "$WORK/repo" remote add public "$REMOTE"
OLD="$(git -C "$WORK/repo" ls-remote --heads public "$BRANCH" | cut -f1)"
if [ "$OLD" = "$NEW" ]; then
  echo "ℹ️  public remote already up to date"
  exit 0
fi
if [ -n "$OLD" ]; then
  git -C "$WORK/repo" fetch -q public "$BRANCH"
  if ! git -C "$WORK/repo" merge-base --is-ancestor "$OLD" "$NEW"; then
    BASE="$(git -C "$WORK/repo" merge-base "$OLD" "$NEW" || true)"
    # -n rather than `| head`: under pipefail, head closing the pipe early kills
    # the script with SIGPIPE before the refusal is printed.
    ONLY_COUNT="$(git -C "$WORK/repo" rev-list --count "${BASE:+$BASE..}$OLD")"
    ONLY_PUBLIC="$(git -C "$WORK/repo" log -n 10 --format='   %h %an: %s' "${BASE:+$BASE..}$OLD")"
    [ "$ONLY_COUNT" -le 10 ] || ONLY_PUBLIC="$ONLY_PUBLIC
   … and $((ONLY_COUNT - 10)) more"
    if [ -z "$FORCE" ]; then
      echo "❌ not a fast-forward: the public '$BRANCH' has $ONLY_COUNT commit(s) this export does not contain:" >&2
      echo "$ONLY_PUBLIC" >&2
      echo "   Either someone committed on the public repo (bring it here with git am), or an" >&2
      echo "   .oss rule changed and rewrote already-published commits. Publishing anyway rewrites" >&2
      echo "   the public history for everyone: re-run with --push --force." >&2
      exit 1
    fi
    echo "⚠️  --force: replacing the public history ($ONLY_COUNT commit(s) there will be discarded)"
    git -C "$WORK/repo" push -q --force-with-lease="$BRANCH:$OLD" public "$BRANCH:$BRANCH"
    echo "🚀 force-pushed $(git -C "$WORK/repo" rev-parse --short "$NEW") to $REMOTE"
    exit 0
  fi
  echo "➕ $(git -C "$WORK/repo" rev-list --count "$OLD..$NEW") new commit(s) since the last export"
fi
git -C "$WORK/repo" push -q public "$BRANCH:$BRANCH"
echo "🚀 pushed $(git -C "$WORK/repo" rev-parse --short "$NEW") to $REMOTE"
