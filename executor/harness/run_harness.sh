#!/usr/bin/env bash
# =====================================================================================
#  run_harness.sh — يبني ملف الـcBot الحقيقي + الشيم ثم يشغّل السيناريوهات.
# -------------------------------------------------------------------------------------
#  المتطلبات:
#    * .NET SDK 8+  (إن لم يكن مثبتاً: https://learn.microsoft.com/dotnet/core/install)
#    * خادم Brain API يعمل محلياً (uvicorn brain.api.main:app --port 8000)
#
#  الاستخدام:
#    ./run_harness.sh                                                 # سيناريوهات التحقق
#    ./run_harness.sh --mode m15 --cycles 5 --interval-seconds 900    # إيقاع M15 حقيقي
#
#  تُمرَّر أي وسائط إضافية إلى الـharness مباشرة، وتتجاوز القيم الافتراضية أدناه
#  (يمكن أيضاً ضبط BASE_URL / SYMBOL / CYCLES / INTERVAL_SECONDS / OUT كمتغيرات بيئة).
# =====================================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOTNET="${DOTNET:-$HOME/.dotnet/dotnet}"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

if [ ! -x "$DOTNET" ]; then
  if command -v dotnet >/dev/null 2>&1; then
    DOTNET=dotnet
  else
    echo "لم يُعثر على dotnet. ثبّت .NET SDK 8 أو مرّر DOTNET=/path/to/dotnet" >&2
    exit 1
  fi
fi

"$DOTNET" build "$HERE/ExecutorHarness/ExecutorHarness.csproj" -v minimal

exec "$DOTNET" "$HERE/ExecutorHarness/bin/Debug/net8.0/ExecutorHarness.dll" \
  --mode "${MODE:-scenarios}" \
  --base-url "${BASE_URL:-http://127.0.0.1:8000}" \
  --symbol "${SYMBOL:-XAUUSD}" \
  --cycles "${CYCLES:-5}" \
  --interval-seconds "${INTERVAL_SECONDS:-900}" \
  --out "${OUT:-$HERE/../out}" \
  "$@"
