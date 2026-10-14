## Numbers

### Editing cells

Select a cell with `click` before you edit it. One click for an empty cell, or to add to what is in it. Three clicks (`--click_count 3`) to replace what is in it.

To change a cell, chain two commands with `&&`: the `click` that selects the cell (one or three clicks), then `type_text` with the new value.

A whole row in one `type_text` call, with a tab between the cells, is a good way to enter many values. Never enter several rows, or several formulas, in one `type_text` call: it fails.

Notes:

- A cell with focus may show a "text entry area" with extra formatting, such as Markdown bold in table headers. Ignore it.
- A cell value is saved at once. Press `Return` only when you are done with the whole spreadsheet.
- A checkbox cell takes 0 (off) or 1 (on) as typed text.
