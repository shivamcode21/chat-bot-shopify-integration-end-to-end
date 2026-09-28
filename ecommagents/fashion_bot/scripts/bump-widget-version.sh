#!/bin/bash
#
# Widget Version Bump Script
#
# Usage:
#   ./scripts/bump-widget-version.sh v2
#   ./scripts/bump-widget-version.sh v3
#
# This script:
# 1. Copies the current latest bundle to new version
# 2. Updates the WIDGET_VERSION constant in the new bundle
# 3. Copies the current latest widget test page when available
# 4. Prints next steps for deployment
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
STATIC_DIR="$ROOT_DIR/static"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get new version from argument
NEW_VERSION="${1:-}"

if [ -z "$NEW_VERSION" ]; then
    echo -e "${RED}❌ Error: Please provide a version number${NC}"
    echo ""
    echo "Usage: $0 <version>"
    echo "Example: $0 v2"
    exit 1
fi

# Validate version format
if [[ ! "$NEW_VERSION" =~ ^v[0-9]+$ ]]; then
    echo -e "${RED}❌ Error: Version must be in format 'vN' (e.g., v1, v2, v3)${NC}"
    exit 1
fi

# Find current latest version
CURRENT_VERSION=""
for v in $(seq 100 -1 1); do
    if [ -f "$STATIC_DIR/chat-widget.v$v.js" ]; then
        CURRENT_VERSION="v$v"
        break
    fi
done

if [ -z "$CURRENT_VERSION" ]; then
    echo -e "${RED}❌ Error: No existing widget bundle found${NC}"
    echo "Please ensure static/chat-widget.v1.js exists"
    exit 1
fi

# Check if new version already exists
if [ -f "$STATIC_DIR/chat-widget.$NEW_VERSION.js" ]; then
    echo -e "${YELLOW}⚠️  Warning: $NEW_VERSION already exists${NC}"
    read -p "Overwrite? (y/N): " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
echo -e "${GREEN}📦 Widget Version Bump${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "Current version: ${YELLOW}$CURRENT_VERSION${NC}"
echo -e "New version:     ${GREEN}$NEW_VERSION${NC}"
echo ""

# Copy bundle
echo "📋 Copying chat-widget.$CURRENT_VERSION.js → chat-widget.$NEW_VERSION.js"
cp "$STATIC_DIR/chat-widget.$CURRENT_VERSION.js" "$STATIC_DIR/chat-widget.$NEW_VERSION.js"

# Update version constant in new bundle
echo "✏️  Updating WIDGET_VERSION constant in new bundle"
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sed -i '' "s/WIDGET_VERSION = '$CURRENT_VERSION'/WIDGET_VERSION = '$NEW_VERSION'/g" "$STATIC_DIR/chat-widget.$NEW_VERSION.js"
else
    # Linux
    sed -i "s/WIDGET_VERSION = '$CURRENT_VERSION'/WIDGET_VERSION = '$NEW_VERSION'/g" "$STATIC_DIR/chat-widget.$NEW_VERSION.js"
fi

# Copy matching widget test page if it exists
CURRENT_TEST_PAGE="$STATIC_DIR/test-widget-$CURRENT_VERSION.html"
NEW_TEST_PAGE="$STATIC_DIR/test-widget-$NEW_VERSION.html"
if [ -f "$CURRENT_TEST_PAGE" ]; then
    echo "🧪 Copying test-widget-$CURRENT_VERSION.html → test-widget-$NEW_VERSION.html"
    cp "$CURRENT_TEST_PAGE" "$NEW_TEST_PAGE"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/$CURRENT_VERSION/$NEW_VERSION/g" "$NEW_TEST_PAGE"
    else
        sed -i "s/$CURRENT_VERSION/$NEW_VERSION/g" "$NEW_TEST_PAGE"
    fi
fi

echo ""
echo -e "${GREEN}✅ Done! New bundle created: static/chat-widget.$NEW_VERSION.js${NC}"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${YELLOW}📋 Next Steps:${NC}"
echo ""
echo "1. Edit the new bundle with your changes:"
echo "   code static/chat-widget.$NEW_VERSION.js"
echo ""
echo "2. Test locally:"
echo "   WIDGET_VERSION=$NEW_VERSION uvicorn fashion_bot.agent_controller:app --reload"
echo ""
echo "3. Verify config endpoint:"
echo "   curl http://localhost:8000/widget/config.json"
echo ""
echo "4. Local test routes will automatically keep only the latest 3 maintained versions"
echo "   when matching test-widget-vN.html files exist."
echo ""
echo "5. Deploy to production:"
echo "   - Set WIDGET_VERSION=$NEW_VERSION in your deployment env"
echo "   - Or update fashion_bot/widget_config.py default"
echo ""
echo "6. Rollback if needed:"
echo "   Set WIDGET_VERSION=$CURRENT_VERSION and redeploy"
echo ""
