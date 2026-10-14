## Notion

### Editing a page

A page is made of blocks. A block can be selected. Press `Return` to edit the text of the selected block.

A new page has an empty title that shows "New page". Type the title, then press `Return` to start the body. To put the cursor into the title, click the text element inside the heading.

Enter one line at a time: `type_text` with the line, then `press_key` with `Return`, chained with `&&`.

Format with Markdown marks. Two of them differ from Markdown: ">" starts a toggle list, and "|" starts a quote.

### Lists

`Return` at the end of a list item starts the next item, with its marker already there. Type only the text of the item, never the marker. To leave the list, press `Return` on an empty item.

### Select all

What `super+a` selects depends on where the cursor is:

- In an empty block: every block of the page.
- In a block with text: the text of that block.
- When the whole text of a block is selected already: every block of the page.

### Placeholders

An empty element shows a placeholder:

- Page title: "New page"
- List item: "List"
- To-do item: "To-do"
- Empty line: a text that starts with "Write,"

It goes away when you type. Never try to select or delete it.

### Code and quote blocks

In a code block `Return` makes a new line inside the block, and `shift+Return` makes one outside it. A quote block is the other way round: `Return` goes outside, `shift+Return` stays inside.

### Moving the cursor

To the top or the bottom: give the page text focus, press `super+a` twice to select the whole page, press `Up` or `Down`, then `Return` to edit the first or the last block.

To one block: press `super+a` twice, then click that block once.

Or search: press `super+f`, type the words, then press `Escape`. The match is now the selected text.
