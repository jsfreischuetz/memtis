#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MEMTIS_KERNEL_DIR=${MEMTIS_KERNEL_DIR:-"${SCRIPT_DIR}/linux"}
MEMTIS_KERNEL_CC=${MEMTIS_KERNEL_CC:-gcc-11}
MEMTIS_KERNEL_JOBS=${MEMTIS_KERNEL_JOBS:-$(nproc)}
MEMTIS_KERNEL_LOCALVERSION=${MEMTIS_KERNEL_LOCALVERSION:--node0}

case "${1:-}" in
    --help|-h)
        cat <<'EOF'
Usage: ./rebuild_kernel.sh [--check]

Build MEMTIS kernel image and header .deb packages using linux/.config.
--check validates the configuration and build tools without building.

Environment overrides:
  MEMTIS_KERNEL_DIR           Kernel source tree (default: ./linux beside script)
  MEMTIS_KERNEL_CC            Compiler (default: gcc-11)
  MEMTIS_KERNEL_JOBS          Parallel jobs (default: nproc)
  MEMTIS_KERNEL_LOCALVERSION  Release suffix (default: -node0)

The script prints installation instructions; it does not install or reboot.
EOF
        exit 0
        ;;
    ""|--check) ;;
    *) echo "ERROR: unknown argument '$1' (use --help)" >&2; exit 1 ;;
esac
if (($# > 1)); then
    echo "ERROR: expected at most one argument (use --help)" >&2
    exit 1
fi

if [[ ! "${MEMTIS_KERNEL_JOBS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: MEMTIS_KERNEL_JOBS must be a positive integer" >&2
    exit 1
fi
if [[ ! "${MEMTIS_KERNEL_LOCALVERSION}" =~ ^-[a-z0-9][a-z0-9.+-]*$ ]]; then
    echo "ERROR: MEMTIS_KERNEL_LOCALVERSION must be a suffix such as -node0" >&2
    exit 1
fi
for required_tool in make "${MEMTIS_KERNEL_CC}" flex bison bc openssl \
    dpkg dpkg-buildpackage dpkg-deb fakeroot rsync; do
    if ! command -v "${required_tool}" >/dev/null 2>&1; then
        echo "ERROR: missing build tool: ${required_tool}" >&2
        echo "Ubuntu dependencies: sudo apt-get install build-essential gcc-11 flex bison bc libssl-dev libelf-dev libncurses-dev dwarves debhelper fakeroot rsync" >&2
        exit 1
    fi
done
if [[ ! -f "${MEMTIS_KERNEL_DIR}/Makefile" || ! -f "${MEMTIS_KERNEL_DIR}/.config" ]]; then
    echo "ERROR: expected a configured kernel source tree at ${MEMTIS_KERNEL_DIR}" >&2
    exit 1
fi
MEMTIS_KERNEL_DIR=$(cd -- "${MEMTIS_KERNEL_DIR}" && pwd)
if ! grep -qx 'CONFIG_HTMM=y' "${MEMTIS_KERNEL_DIR}/.config"; then
    echo "ERROR: ${MEMTIS_KERNEL_DIR}/.config must contain CONFIG_HTMM=y" >&2
    exit 1
fi

echo "Kernel source: ${MEMTIS_KERNEL_DIR}"
echo "Compiler: ${MEMTIS_KERNEL_CC}; jobs: ${MEMTIS_KERNEL_JOBS}; suffix: ${MEMTIS_KERNEL_LOCALVERSION}"
if [[ "${1:-}" == --check ]]; then
    echo "MEMTIS configuration and build-tool checks passed."
    exit 0
fi

config_backup=$(mktemp "${MEMTIS_KERNEL_DIR}/.config.before-rebuild.XXXXXX")
cp -- "${MEMTIS_KERNEL_DIR}/.config" "${config_backup}"
echo "Configuration backup: ${config_backup}"

make_args=(-C "${MEMTIS_KERNEL_DIR}" "CC=${MEMTIS_KERNEL_CC}" \
    "LOCALVERSION=${MEMTIS_KERNEL_LOCALVERSION}")
make "${make_args[@]}" olddefconfig
grep -qx 'CONFIG_HTMM=y' "${MEMTIS_KERNEL_DIR}/.config" || {
    echo "ERROR: CONFIG_HTMM is not enabled after olddefconfig" >&2
    exit 1
}
kernel_release=$(make --no-print-directory -s "${make_args[@]}" kernelrelease)
package_version="${kernel_release}-$(date -u +%Y%m%d%H%M%S)"
package_arch=$(dpkg --print-architecture)
package_dir=$(dirname -- "${MEMTIS_KERNEL_DIR}")
build_log="${package_dir}/build-${package_version}.log"

echo "Building ${kernel_release}; log: ${build_log}"
# bindeb-pkg includes modules and preserves existing objects for incremental builds.
make "${make_args[@]}" -j"${MEMTIS_KERNEL_JOBS}" \
    "KDEB_PKGVERSION=${package_version}" bindeb-pkg 2>&1 | tee "${build_log}"

image_package="${package_dir}/linux-image-${kernel_release}_${package_version}_${package_arch}.deb"
headers_package="${package_dir}/linux-headers-${kernel_release}_${package_version}_${package_arch}.deb"
for package in "${image_package}" "${headers_package}"; do
    if [[ ! -s "${package}" ]]; then
        echo "ERROR: expected package not found: ${package}" >&2
        exit 1
    fi
done

printf '\nBuild complete. Install the image (including modules) and headers with:\n'
printf 'sudo dpkg -i -- %q %q\n' "${image_package}" "${headers_package}"
printf '\nAfter installation, select Linux %s in GRUB when rebooting.\n' "${kernel_release}"
printf 'Confirm that uname -r reports %s before running measurements.\n' "${kernel_release}"
