#!/bin/sh
set -eu

if [ -z "${QNX_SDK_PATH:-}" ]; then
    printf '%s\n' 'Set QNX_SDK_PATH to the QNX 6.5 SDK root before building.' >&2
    exit 1
fi

sdk_root=$(cd "$QNX_SDK_PATH" && pwd)
host_candidates=
case "$(uname -s)" in
    Linux) host_candidates="linux/x86_64 linux/x86" ;;
    Darwin) host_candidates="darwin/x86_64 darwin/x86" ;;
    *)
        printf '%s\n' 'Run native/build.ps1 on Windows or native/build.sh on Linux/macOS.' >&2
        exit 1
        ;;
esac

compiler=
for host in $host_candidates; do
    candidate="$sdk_root/host/$host/usr/bin/ntoarmv7-gcc"
    if [ -x "$candidate" ]; then
        compiler=$candidate
        qnx_host="$sdk_root/host/$host"
        break
    fi
done
if [ -z "$compiler" ]; then
    printf 'QNX 6.5 ARMv7 compiler not found under %s/host.\n' "$sdk_root" >&2
    exit 1
fi

export QNX_HOST="$qnx_host"
export QNX_TARGET="$sdk_root/target/qnx6"
cd "$(dirname "$0")"
"$compiler" -D_QNX_SOURCE -std=c99 -O2 -Wall -Wextra \
    -march=armv7-a -mfloat-abi=softfp -mfpu=neon -lsocket \
    hu_screen_mirror.c -o hu-screen-mirror
printf '%s\n' 'Built hu-screen-mirror'
