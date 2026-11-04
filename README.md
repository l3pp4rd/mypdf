# mypdf

A lightweight PDF reader for Linux written in a single Python file (GTK 3 + Poppler).

## Requirements (Ubuntu/Debian)

    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-poppler-0.18

## Run / install

    ./mypdf.py file.pdf      # run directly
    ./install.sh             # installs to ~/.local/bin + app menu entry

## Usage

| Action | How |
|---|---|
| Open | Ctrl+O, the toolbar button, a command-line argument, or drag a file onto the window |
| Select text | Drag with the mouse (selection can span pages); double-click selects a word, triple-click a line, Shift+click extends the selection |
| Copy | Ctrl+C or right-click → Copy (a selection also goes to the middle-click clipboard) |
| Select all | Ctrl+A |
| Print | Ctrl+P (system print dialog; "Print to File" produces a PDF) |
| Search | Ctrl+F (pre-fills with selected text); Enter / Shift+Enter, F3 / Shift+F3 or Ctrl+G / Ctrl+Shift+G for next/previous match; Esc closes |
| Links | Click to follow (hover shows the target). Web/mail links open in your browser/mail app; links inside the document jump there; Alt+Left goes back |
| Zoom | Ctrl +/−, Ctrl+mouse wheel, Ctrl+0 fit width, Ctrl+1 100% |
| Navigate | Type a page number in the header bar; PgUp/PgDn, Space, arrows, Home/End |
| Outline | F9 or the sidebar button shows the document's table of contents; click an entry to jump there |
| Rotate | Ctrl+Left / Ctrl+Right (view only; printing is unaffected) |
| Fullscreen | F11; Esc leaves |
| Presentation | F5: one page per screen on black. Next: Space, Enter, →, ↓, PgDn, click, wheel. Previous: Shift+Space, Backspace, ←, ↑, PgUp, right-click. Home/End, Esc or F5 to leave |
| Menu | The ☰ button (or right-click on the page) lists outline, rotation, fullscreen, presentation and print |
| Close | Ctrl+W / Ctrl+Q |

Password-protected PDFs prompt for the password. For safety, only http(s), mailto and ftp links are opened;
links that would launch programs or run scripts are ignored.

Each document reopens where you left it (page, zoom, rotation and whether the outline
was shown). This is stored in `~/.local/state/mypdf/positions.json` (the last 1000 files);
delete that file to forget the history.
