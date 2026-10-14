## Slack

When no text field has focus, Slack puts typed text into the message composer. Before you press `Return`, check that the field you mean has focus, or you may send a message by accident.

Write into the message composer with `set_value`, not `type_text`: a text of several lines then cannot be sent by accident. `set_value` writes Markdown marks as plain characters. To turn them into formatting afterwards, press `super+shift+f`.

A text that contains a newline:

- `set_value` adds a new line and does not send.
- `type_text` **sends** the message.

If you must make a new line with `type_text` or `press_key`: each person chooses in Slack whether `Return` or `shift+Return` sends. The text from `get_app_state` tells you which. A button below the composer carries the hint "(key combination) to add a new line". The other combination sends. The hint is there only when the composer holds 3 or more characters, and only in the composer under a channel or a direct message, not in thread replies or edits.

If the text from `get_app_state` on Slack does not behave as expected, trust the screenshot.
