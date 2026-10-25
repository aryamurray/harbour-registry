# harbour-registry-local

A **local, unpublished** Harbour registry: a git repository in Harbour's registry
layout, holding real C libraries as pins + patches (no upstream source is vendored).
It is generated, never hand-edited.

**Serves:** zlib 1.3.1, libpng 1.6.50, freetype 2.14.3, harfbuzz 15.0.0, cjson 1.7.18,
libuv 1.51.0, zstd 1.5.7, curl 8.22.0, openssl 3.5.4 (needs `perl` on PATH).
Not yet: CPython (its entry is not merged into the tool yet).

## Use it

Add to Harbour's global config (on macOS: `~/Library/Caches/com.harbour.harbour/config.toml`;
a non-empty list REPLACES the built-in registries, so keep those listed after it):

```toml
[[registries]]
name = "local"
url = "file:///Users/clarkyy/Documents/personal/code/harbour-registry-local"
priority = -10        # lower wins; the first registry containing a package supplies it
```

Then `harbour add zlib`, `harbour add libpng`, ... `harbour registry list` shows the order.

## Layout (one paragraph)

`index/<letter>/<name>.idx` is the **tier-1 index** (one JSON line per version: deps,
checksum, shim path) that Harbour resolves from. `index/<letter>/<name>/<version>.toml` is the
**tier-2 shim** (source pin: git url+rev, or tarball url+sha256+strip_prefix; plus its
patches) and `<version>.patch` is the patch that adds the package's `Harbour.toml`
(and any overlay such as libpng's `pnglibconf.h`). `.gitattributes` carries
`*.patch -text` so line-ending conversion cannot change a patch's bytes (Harbour hashes them
exactly). **Do not remove it.**

## Refresh or extend (also the way to pick up a registry format change)

Everything here is the output of `tools/registry-from-shims` plus `harbour registry index`.
After a format change, or to add a package, re-run the tool from the harbour repo into a FRESH
directory and replace this repo's contents with its output:

```sh
H=~/Documents/personal/code/harbour
python3 $H/tools/registry-from-shims/make_registry.py /tmp/new-registry $H/target/debug/harbour \
    --cache /tmp/registry-cache --tarball zlib
rsync -a --delete /tmp/new-registry/index/ index/ && cp /tmp/new-registry/config.toml .
harbour registry index .      # rebuild the tier-1 index with the current binary
git add -A && git commit -m "regenerate"
```

To add a package, add a row to `PACKAGES` (and `TARBALLS`) in `make_registry.py`; its
manifest goes in `tools/registry-from-shims/manifests/`.
