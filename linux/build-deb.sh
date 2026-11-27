#!/usr/bin/env bash
# Build the Linux .deb for Debian 13 (trixie) / amd64 from the HEAD commit of this checkout. Run from anywhere.
#
#   linux/build-deb.sh [--no-shader-pack]
#
# Output: publish/Linux/jellyhome-player_<version>-1_amd64.deb  (+ .sha256 and a build manifest)
# Needs: git, dpkg-deb, python3 (3.13) with pip + setuptools>=77, network (PyPI for the hash-locked wheels, GitHub for
# the shader pack). Installs nothing on this machine: everything happens in a temp directory.
#
# Source: a release build is made from ONE commit. Every path the build reads (SOURCE_PATHS below) is exported from
# HEAD with `git archive`, and the build refuses to start while any of them differs from HEAD (a modified, staged,
# deleted or untracked file), so neither an edit nor an untracked file can reach the package and the package is the
# commit the manifest names. Nothing in the working tree is changed, stashed or deleted; the refusal lists the paths.
# Untracked or modified files OUTSIDE those paths (docs/, tests/, other tools/, .claude/, ...) do not block the
# build and cannot reach it. DEB_ALLOW_DIRTY=1 is a developer override that builds the working tree as it is
# (tracked + untracked-but-not-ignored files, as before): it warns, it is refused in CI, and the package is marked
# as not a release -- Debian revision "1~dirty" (which sorts below "1", so a release always upgrades over it, and
# artifacts.sh never collects it), a line in its description, and "release build: NO" in the manifest.
#
# Reproducibility: dependencies come from linux/requirements-*.lock with --require-hashes, file mtimes are fixed to
# SOURCE_DATE_EPOCH (default: the commit time of HEAD), bytecode is compiled with checked-hash invalidation, and the
# archive is written with root ownership. The shader pack is PINNED below to a release tag and the SHA-256 of its
# extracted tree (a different tag needs SHADER_PACK_TAG and SHADER_PACK_TREE_SHA256 together); the commit, its tree,
# a hash of the staged application source, the tag, the pip/setuptools/wheel versions and the lock file's hash go in
# the build manifest.
#
# Environment: OUT (output directory), SOURCE_DATE_EPOCH, SHADER_PACK_TAG + SHADER_PACK_TREE_SHA256,
# DEB_MAINTAINER ("Name <address>"; the default is a placeholder and the build says so), DEB_ALLOW_DIRTY (above), and
# DEB_STOP_AFTER_STAGING=<dir> (tests only: stage the application source, write <dir>/stage.txt and <dir>/src, and
# stop before anything is built).
set -euo pipefail

#: The shader pack release this package is built with. Bump both together, after looking at the release.
PINNED_SHADER_PACK_TAG="v3.0.0"
PINNED_SHADER_PACK_TREE_SHA256="e950d3833a25045a37af18bc2c445c03d79bd4a7eb1189be4835206603991224"
PLACEHOLDER_MAINTAINER="JellyHome Player contributors <braisravina@users.noreply.github.com>"

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
cd "$root"

with_shaders=1
[ "${1:-}" = "--no-shader-pack" ] && with_shaders=0

#: Every path of the source tree this build reads: the application (APP_PATHS, which becomes the wheel), the notices,
#: this directory (the launcher, the lock file and this script), and the four build helpers it runs.
APP_PATHS=(jellyfin_mpv_shim pyproject.toml MANIFEST.in README.md LICENSE.md)
SOURCE_PATHS=("${APP_PATHS[@]}" THIRD_PARTY_NOTICES.md linux
              tools/msgfmt.py tools/po_lint.py tools/gen_locale_index.py tools/collect_licenses.py)

echo "== 0/6 select the source"
command -v git > /dev/null || { echo "error: git is required" >&2; exit 1; }
git rev-parse --is-inside-work-tree > /dev/null 2>&1 \
    || { echo "error: $root is not a git checkout; a package is built from a commit" >&2; exit 1; }
commit="$(git rev-parse --verify 'HEAD^{commit}')"
source_tree="$(git rev-parse --verify "$commit^{tree}")"
allow_dirty="${DEB_ALLOW_DIRTY:-}"
case "$allow_dirty" in
    ""|1) ;;
    *) echo "error: DEB_ALLOW_DIRTY must be 1 or unset (got '$allow_dirty')" >&2; exit 1 ;;
esac
if [ "$allow_dirty" = 1 ] && { [ "${CI:-}" = true ] || [ -n "${GITHUB_ACTIONS:-}" ]; }; then
    echo "error: DEB_ALLOW_DIRTY is a developer override and is refused in CI" >&2
    exit 1
fi
# Modified, staged, deleted and untracked-but-not-ignored files under the source paths, NUL-separated so that no
# file name can split or merge an entry. --no-optional-locks: a check must not write the index.
dirty="$(git --no-optional-locks status --porcelain=v1 -z --untracked-files=all -- "${SOURCE_PATHS[@]}" | tr '\0' '\n')"
if [ -z "$dirty" ]; then
    release=1
    echo "   release build of commit $commit"
elif [ "$allow_dirty" = 1 ]; then
    release=0
    echo "WARNING: DEB_ALLOW_DIRTY=1 -- building the WORKING TREE, which differs from commit $commit:" >&2
    printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    echo "WARNING: this package is NOT a release and NOT reproducible (Debian revision 1~dirty). Do not distribute it." >&2
else
    echo "error: these packaged paths differ from commit $commit:" >&2
    printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    echo "  A release package is built from a commit: commit these changes, or move them out of the packaged paths." >&2
    echo "  Nothing was changed. For a throw-away developer build of the working tree, set DEB_ALLOW_DIRTY=1." >&2
    exit 2
fi
epoch="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct "$commit")}"
export SOURCE_DATE_EPOCH="$epoch"

work="$(mktemp -d)"
# ${work:?} makes an empty value a hard error instead of an rm of the wrong thing.
trap 'rm -rf -- "${work:?}"' EXIT
src="$work/src"; pkg="$work/pkg"; lib="$pkg/usr/lib/jellyhome-player"
mkdir -p "$src/tools" "$lib" "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/usr/share/applications" \
         "$pkg/usr/share/metainfo" "$pkg/usr/share/doc/jellyhome-player"

if [ "$release" = 1 ]; then
    # Everything below reads the commit, not the working tree: the source paths are exported from it and the
    # build runs inside the export. (This script itself is the working tree's copy, which the check above has
    # just shown to be the commit's.)
    tree="$work/tree"; mkdir -p "$tree"
    git archive --format=tar "$commit" -- "${SOURCE_PATHS[@]}" | tar -x -C "$tree"
    list_app_files() { git -C "$root" ls-tree -r -z --name-only "$commit" -- "${APP_PATHS[@]}"; }
else
    tree="$root"
    # tracked + new-but-not-ignored, as the working tree has them. dist/, publish/, *.mo, the shader pack and the
    # local test state are gitignored, so they cannot leak in this way.
    list_app_files() { git -C "$root" ls-files -z -co --exclude-standard -- "${APP_PATHS[@]}"; }
fi
cd "$tree"
here="$tree/linux"

version="$(grep '^CLIENT_VERSION' jellyfin_mpv_shim/constants.py | cut -d '"' -f 2)"
# Debian version: a pre-release sorts before the release ("3.3.0~pre1"); the revision is ours. A developer build of
# a dirty tree is revision "1~dirty", which sorts BELOW the release, so it can never stand in for one.
debver="$(printf '%s' "$version" | sed 's/pre/~pre/')-1"
[ "$release" = 1 ] || debver="${debver}~dirty"
appid="io.github.braisravina.JellyHomePlayer"
arch="amd64"
lock="$here/requirements-linux-cp313-amd64.lock"
out="${OUT:-$root/publish/Linux}"

echo "== 1/6 stage the application source (allow-list)"
while IFS= read -r -d '' f; do
    case "$f" in
        *.pyc|*/__pycache__/*|*.orig|*.rej) continue ;;
    esac
    [ -f "$f" ] || continue
    mkdir -p -- "$src/$(dirname -- "$f")"; cp -p -- "$f" "$src/$f"
done < <(list_app_files)
cp -p tools/msgfmt.py tools/po_lint.py tools/gen_locale_index.py "$src/tools/"
# One hash for the staged application source -- paths and contents, before anything is compiled or added -- so the
# manifest says exactly what went into the wheel, and a rebuild of the same commit can be compared without unpacking.
staged_sha256="$(python3 - "$src" <<'PY'
import hashlib, os, sys
base = sys.argv[1]
h = hashlib.sha256()
for dp, dn, fn in os.walk(base):
    dn.sort()
    for f in sorted(fn):
        p = os.path.join(dp, f)
        rel = os.path.relpath(p, base)
        if rel.startswith("tools" + os.sep):
            continue
        with open(p, "rb") as fh:
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0" + hashlib.sha256(fh.read()).digest())
print(h.hexdigest())
PY
)"
if [ -n "${DEB_STOP_AFTER_STAGING:-}" ]; then
    mkdir -p -- "$DEB_STOP_AFTER_STAGING"
    cp -r -- "$src" "$DEB_STOP_AFTER_STAGING/src"
    {
        echo "commit: $commit"
        echo "tree: $source_tree"
        echo "release: $release"
        echo "debver: $debver"
        echo "staged source sha256: $staged_sha256"
    } > "$DEB_STOP_AFTER_STAGING/stage.txt"
    echo "stopped after staging (DEB_STOP_AFTER_STAGING); nothing was built"
    exit 0
fi

for tool in dpkg-deb python3 tar; do
    command -v "$tool" > /dev/null || { echo "error: $tool is required" >&2; exit 1; }
done
py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[ "$py" = "3.13" ] || { echo "error: the lock file is for CPython 3.13 (this is $py); the package targets Debian 13" >&2; exit 1; }

echo "== 2/6 compile translations (same filter + compiler as gen_pkg.sh; no GNU gettext needed)"
(
    cd "$src"
    n=0
    while read -r po; do
        tmp="$(mktemp)"
        if python3 tools/po_lint.py --filter "$po" -o "$tmp"; then srcpo="$tmp"; else srcpo="$po"; fi
        python3 tools/msgfmt.py "$srcpo" -o "${po%.*}.mo"
        rm -f -- "$tmp"
        n=$((n+1))
    done < <(find jellyfin_mpv_shim -name '*.po' | sort)
    echo "   compiled $n catalogs"
    python3 tools/gen_locale_index.py > /dev/null 2>&1 || echo "   (language index not regenerated; the committed one is used)"
)

shader_tag="none"
if [ "$with_shaders" = 1 ]; then
    echo "== 3/6 shader pack"
    shader_tag="${SHADER_PACK_TAG:-$PINNED_SHADER_PACK_TAG}"
    if [ "$shader_tag" = "$PINNED_SHADER_PACK_TAG" ]; then
        want_tree="${SHADER_PACK_TREE_SHA256:-$PINNED_SHADER_PACK_TREE_SHA256}"
    else
        want_tree="${SHADER_PACK_TREE_SHA256:-}"
        [ -n "$want_tree" ] || { echo "error: SHADER_PACK_TAG=$shader_tag needs SHADER_PACK_TREE_SHA256 as well" >&2; exit 1; }
    fi
    curl -fsSL "https://github.com/iwalton3/default-shader-pack/archive/$shader_tag.zip" -o "$work/sp.zip"
    python3 -m zipfile -e "$work/sp.zip" "$work/sp"
    got_tree="$(python3 - "$work"/sp/default-shader-pack-* <<'PY'
import hashlib, os, sys
base = sys.argv[1]
h = hashlib.sha256()
for dp, dn, fn in os.walk(base):
    dn.sort()
    for f in sorted(fn):
        p = os.path.join(dp, f)
        with open(p, "rb") as fh:
            h.update(os.path.relpath(p, base).encode() + b"\0" + hashlib.sha256(fh.read()).digest())
print(h.hexdigest())
PY
)"
    if [ "$got_tree" != "$want_tree" ]; then
        echo "error: shader pack $shader_tag does not match its pinned hash" >&2
        echo "       expected $want_tree" >&2
        echo "       got      $got_tree" >&2
        exit 1
    fi
    mkdir -p "$src/jellyfin_mpv_shim/default_shader_pack"
    cp -r "$work"/sp/default-shader-pack-*/. "$src/jellyfin_mpv_shim/default_shader_pack/"
    echo "   default-shader-pack $shader_tag (tree sha256 verified)"
else
    echo "== 3/6 shader pack: skipped (--no-shader-pack); this build has no video profiles"
fi

echo "== 4/6 build the application wheel and install it with the hash-locked libraries"
python3 -m pip wheel --quiet --no-deps --no-build-isolation -w "$work/wheel" "$src"
python3 -m pip install --quiet --no-deps --no-compile --target "$lib" "$work"/wheel/jellyhome_player-*.whl
python3 -m pip install --quiet --no-deps --no-compile --require-hashes --target "$lib" -r "$lock"
rm -rf -- "${lib:?}/bin"
# pip records where it installed from (a temp path of this build) in direct_url.json and RECORD: drop both so the
# output does not depend on the build directory.
for du in "$lib"/*.dist-info/direct_url.json; do
    [ -e "$du" ] || continue
    rec="$(dirname "$du")/RECORD"
    rm -f -- "$du"
    grep -v 'direct_url.json,' "$rec" > "$rec.new" || true
    mv "$rec.new" "$rec"
done
find "$lib" -name '__pycache__' -type d -prune -exec rm -rf {} +
# What a user needs, asserted rather than hoped for.
for must in jellyfin_mpv_shim/mpv_shim.py mpv.py 'PIL/_imagingft*.so' jellyfin_mpv_shim/mouse.lua; do
    # shellcheck disable=SC2086
    ls "$lib"/$must > /dev/null 2>&1 || { echo "error: $must missing from the bundle" >&2; exit 1; }
done
# ...and what must never ship.
if find "$lib" \( -name 'cred*.json' -o -name 'users.json*' -o -name 'conf.json' -o -name '*.db' -o -name 'log.txt' \) | grep -q .; then
    echo "error: a credential/state-shaped file is in the bundle" >&2; exit 1
fi

echo "== 5/6 licences, desktop integration, launcher"
doc="$pkg/usr/share/doc/jellyhome-player"
python3 tools/collect_licenses.py "$lib" "$doc/licenses" --table "$doc/python-licences.md" > "$work/licence-summary.txt"
cp LICENSE.md "$doc/LICENSE.md"
cp THIRD_PARTY_NOTICES.md "$doc/THIRD_PARTY_NOTICES.md"
if [ -f "$lib/jellyfin_mpv_shim/default_shader_pack/LICENSE.md" ]; then
    cp "$lib/jellyfin_mpv_shim/default_shader_pack/LICENSE.md" "$doc/shader-pack-LICENSE.md"
fi
cat > "$doc/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: JellyHome Player
Source: https://github.com/braisravina/jellyhome-player

Files: *
Copyright: 2019-2026 Izzie Walton and contributors; 2014 Weston Nielson; JellyHome Player contributors
License: GPL-3.0+
 Fork of Jellyfin MPV Shim. The full text is in LICENSE.md in this directory (GPLv3 for the work as a whole, with
 the MIT-licensed Plex MPV Shim / Omplex portions it incorporates reproduced there).

Files: usr/lib/jellyhome-player/*
Copyright: see licenses/<package>/ in this directory for each bundled Python library
License: see THIRD_PARTY_NOTICES.md and python-licences.md in this directory
 Bundled libraries are used under their own licences, all compatible with GPL-3.0+. Pillow's wheel also carries
 FreeType, HarfBuzz, libpng and others, whose notices are in licences/pillow/LICENSE.
EOF
install -m 644 "jellyfin_mpv_shim/integration/$appid.desktop" "$pkg/usr/share/applications/$appid.desktop"
install -m 644 "jellyfin_mpv_shim/integration/$appid.appdata.xml" "$pkg/usr/share/metainfo/$appid.metainfo.xml"
for sz in 16 32 48 64 128 256; do
    d="$pkg/usr/share/icons/hicolor/${sz}x${sz}/apps"
    mkdir -p "$d"
    install -m 644 "jellyfin_mpv_shim/integration/jellyfin-$sz.png" "$d/$appid.png"
done
install -m 755 "$here/jellyhome-player" "$pkg/usr/bin/jellyhome-player"

# Byte-compile for the one interpreter this package targets, deterministically (checked-hash, no mtimes).
# -s strips the staging prefix so co_filename is the install path (/usr/lib/...), not this build's temp directory.
python3 -m compileall -q -j 1 --invalidation-mode checked-hash -s "$pkg" "$lib" > /dev/null

echo "== 6/6 control files and the archive"
installed_kib="$(du -sk "$pkg" | cut -f1)"
maintainer="${DEB_MAINTAINER:-$PLACEHOLDER_MAINTAINER}"
if [ "$maintainer" = "$PLACEHOLDER_MAINTAINER" ]; then
    echo "   note: the Maintainer field is a placeholder; set DEB_MAINTAINER=\"Name <address>\" for a release build" >&2
fi
cat > "$pkg/DEBIAN/control" <<EOF
Package: jellyhome-player
Version: $debver
Section: video
Priority: optional
Architecture: $arch
Depends: python3 (>= 3.13), python3 (<< 3.14), libmpv2 (>= 0.38), libfribidi0, libc6 (>= 2.34)
Recommends: fonts-dejavu-core | fonts-noto-core, python3-pystray, zenity | kdialog, xdg-utils
Suggests: fonts-noto-cjk, fonts-noto-color-emoji
Installed-Size: $installed_kib
Maintainer: $maintainer
Homepage: https://github.com/braisravina/jellyhome-player
Description: MPV-based Jellyfin desktop client for easy home streaming
 JellyHome Player is an independent fork of Jellyfin MPV Shim: browse and play
 a Jellyfin library in an mpv window, with Direct Play, offline sync, casting
 and SyncPlay. Privacy-focused defaults: no third-party lookups, no background
 update checks, no autostart.
 .
 Settings, logins and caches are kept per user in ~/.config/jellyhome-player and
 ~/.cache/jellyhome-player; removing the package does not touch them.
EOF
if [ "$release" != 1 ]; then
    cat >> "$pkg/DEBIAN/control" <<EOF
 .
 DEVELOPER BUILD of a working tree that differs from commit $commit.
 Not a release, not reproducible; do not distribute.
EOF
fi
cat > "$pkg/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
fi
exit 0
EOF
cat > "$pkg/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    remove|purge)
        if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
        # Home directories are never touched here: settings, logins and caches belong to the user.
        ;;
esac
exit 0
EOF
chmod 755 "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/postrm"

# Deterministic metadata: fixed mtimes, normal modes.
find "$pkg" -exec touch -h -d "@$epoch" {} +
find "$pkg" -type d -exec chmod 755 {} +
find "$pkg" -type f ! -perm -u+x -exec chmod 644 {} +
mkdir -p "$out"
deb="$out/jellyhome-player_${debver}_${arch}.deb"
rm -f -- "$deb"
dpkg-deb --root-owner-group -Zxz -z6 --build "$pkg" "$deb" > /dev/null
( cd "$out" && sha256sum "$(basename "$deb")" > "$(basename "$deb").sha256" )
{
    echo "package: jellyhome-player $debver ($arch)"
    if [ "$release" = 1 ]; then
        echo "release build: yes (every packaged source file is from the commit below)"
    else
        echo "release build: NO - developer build of a dirty working tree (DEB_ALLOW_DIRTY=1); not reproducible"
    fi
    echo "source commit: $commit"
    echo "source tree: $source_tree"
    echo "staged source sha256: $staged_sha256"
    if [ "$release" != 1 ]; then
        echo "differs from the commit:"
        printf '%s\n' "$dirty" | sed 's/^/  /'
    fi
    echo "SOURCE_DATE_EPOCH: $epoch"
    echo "python (build): $(python3 --version)"
    echo "build tools: $(python3 -m pip --version | cut -d' ' -f1-2), setuptools $(python3 -c 'import setuptools; print(setuptools.__version__)'), wheel $(python3 -c 'import wheel; print(wheel.__version__)')"
    echo "maintainer: $maintainer"
    echo "shader pack: $shader_tag"
    echo "lock file sha256: $(sha256sum "$lock" | cut -d' ' -f1)"
    echo "bundled libraries:"
    sed 's/^/  /' "$work/licence-summary.txt"
} > "$out/$(basename "$deb").manifest.txt"
echo
echo "built $deb ($(du -h "$deb" | cut -f1))"
cat "$deb.sha256"
