#!/bin/sh
# Installs mypdf for the current user (no root needed).
set -e
here=$(cd "$(dirname "$0")" && pwd)
install -Dm755 "$here/mypdf.py" "$HOME/.local/bin/mypdf"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/mypdf.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=mypdf
Comment=Lightweight PDF reader
Exec=$HOME/.local/bin/mypdf %f
Icon=x-office-document
MimeType=application/pdf;
Categories=Office;Viewer;
Terminal=false
DESKTOP
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed: $HOME/.local/bin/mypdf"
echo "To make it your default PDF viewer run:"
echo "  xdg-mime default mypdf.desktop application/pdf"
