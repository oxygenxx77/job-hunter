#!/bin/bash
# 编译两只桌宠并放到桌面：秋招看板.app（小招）、求职助手.app（小航）
# 用法：bash desktop-pet/build.sh            编译并安装到桌面
#      bash desktop-pet/build.sh --no-install 只编译到 desktop-pet/build
set -euo pipefail
cd "$(dirname "$0")"

BUILD="build"
rm -rf "$BUILD"
mkdir -p "$BUILD"

echo "编译中…"
swiftc -O -target "$(uname -m)-apple-macos13.0" main.swift -o "$BUILD/Pet"

make_app() {
  local kind="$1" name="$2" exe="$3" bundle_id="$4" pet="$5"
  local app="$BUILD/$name.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  cp "$BUILD/Pet" "$app/Contents/MacOS/$exe"

  # 用桌宠自己的样子做图标
  local iconset="$BUILD/$kind.iconset"
  mkdir -p "$iconset"
  "$BUILD/Pet" --render-icon "$kind" "$BUILD/$kind.png"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$BUILD/$kind.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$BUILD/$kind.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"

  cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>$exe</string>
  <key>CFBundleIdentifier</key><string>$bundle_id</string>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundleDisplayName</key><string>$name</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>$pet</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>PetKind</key><string>$kind</string>
</dict>
</plist>
EOF
  codesign --force --sign - "$app" >/dev/null 2>&1 || true
  echo "  ✓ $name.app（$pet）"
}

make_app kanban "秋招看板" "KanbanPet" "io.github.oxygenxx77.pet.kanban" "小招"
make_app jobhunter "求职助手" "JobHunterPet" "io.github.oxygenxx77.pet.jobhunter" "小航"

if [ "${1:-}" = "--no-install" ]; then
  echo "已编译到 $(pwd)/$BUILD"
  exit 0
fi

# 换新版本前先让正在运行的桌宠下班（它们自己启动的服务也会一起关掉），装好后再叫回来
relaunch=()
for pair in "KanbanPet:秋招看板" "JobHunterPet:求职助手"; do
  exe="${pair%%:*}"
  if pgrep -x "$exe" >/dev/null; then
    relaunch+=("${pair#*:}")
    pkill -x "$exe" || true
  fi
done
for _ in $(seq 1 40); do
  pgrep -x KanbanPet >/dev/null || pgrep -x JobHunterPet >/dev/null || break
  sleep 0.25
done

for name in "秋招看板" "求职助手"; do
  rm -rf "$HOME/Desktop/$name.app"
  cp -R "$BUILD/$name.app" "$HOME/Desktop/$name.app"
done
echo "已放到桌面：秋招看板.app、求职助手.app"

for name in "${relaunch[@]+"${relaunch[@]}"}"; do
  open "$HOME/Desktop/$name.app" --args --quiet
done
