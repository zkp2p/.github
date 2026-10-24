#!/usr/bin/env bash
# Build the upstream CVE-2026-111497 fix while Ubuntu's package is pending.
# This builds packages only; installation is an explicit host operation.
set -euo pipefail
umask 022
export SOURCE_DATE_EPOCH=1792588346

if [ "$#" -ne 1 ]; then
  echo "usage: $0 OUTPUT_DIRECTORY" >&2
  exit 2
fi
output_dir="$(realpath -m "$1")"
mkdir -p "$output_dir"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT

# google/brotli v1.1.1; retain the 1.1 ABI and only its security patch release.
revision=db0631dfea829194206d269bc7b25d0908cdb97d
checksum=38f9c17ee36c2e930ce00975b50fcd246a8e5594c1bcf43cd56b14118786b82a
version=1.1.1-0peer1
architecture="$(dpkg --print-architecture)"
multiarch="$(gcc -dumpmachine)"
curl --fail --location --proto '=https' --tlsv1.2 \
  "https://github.com/google/brotli/archive/$revision.tar.gz" \
  --output "$build_dir/source.tar.gz"
printf '%s  %s\n' "$checksum" "$build_dir/source.tar.gz" | sha256sum --check
tar -xzf "$build_dir/source.tar.gz" -C "$build_dir"
source_dir="$build_dir/brotli-$revision"
cmake -S "$source_dir" -B "$build_dir/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_INSTALL_LIBDIR="lib/$multiarch"
cmake --build "$build_dir/build" --parallel 2
ctest --test-dir "$build_dir/build" --output-on-failure
DESTDIR="$build_dir/stage" cmake --install "$build_dir/build"
cmake -S "$source_dir" -B "$build_dir/static" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DBROTLI_DISABLE_TESTS=ON
cmake --build "$build_dir/static" --parallel 2

runtime="$build_dir/libbrotli1"
development="$build_dir/libbrotli-dev"
mkdir -p "$runtime/DEBIAN" "$runtime/usr/lib/$multiarch" \
  "$development/DEBIAN" "$development/usr/lib/$multiarch"
mv "$build_dir/stage/usr/lib/$multiarch/"*.so.1* "$runtime/usr/lib/$multiarch/"
mv "$build_dir/stage/usr/lib/$multiarch/"*.so "$development/usr/lib/$multiarch/"
mv "$build_dir/stage/usr/lib/$multiarch/pkgconfig" "$development/usr/lib/$multiarch/"
cp "$build_dir/static/"libbrotli*.a "$development/usr/lib/$multiarch/"
mv "$build_dir/stage/usr/include" "$development/usr/"
for package in libbrotli1 libbrotli-dev; do
  package_dir="$build_dir/$package"
  mkdir -p "$package_dir/usr/share/doc/$package"
  cp "$source_dir/LICENSE" "$package_dir/usr/share/doc/$package/copyright"
  cat > "$package_dir/DEBIAN/control" <<EOF
Package: $package
Version: $version
Architecture: $architecture
Maintainer: Kohai <kohai@zkp2p.xyz>
Section: libs
Priority: optional
Description: Brotli 1.1.1 security update for CVE-2026-111497
 Upstream commit $revision, checksum verified and upstream tests passed.
EOF
done
printf 'Depends: libc6 (>= 2.29)\n' >> "$runtime/DEBIAN/control"
printf 'Depends: libbrotli1 (= %s)\n' "$version" >> "$development/DEBIAN/control"
printf 'activate-noawait ldconfig\n' > "$runtime/DEBIAN/triggers"
dpkg-deb --root-owner-group --build "$runtime" "$output_dir/libbrotli1_${version}_${architecture}.deb"
dpkg-deb --root-owner-group --build "$development" "$output_dir/libbrotli-dev_${version}_${architecture}.deb"
sha256sum "$output_dir/"*"_${version}_${architecture}.deb"
