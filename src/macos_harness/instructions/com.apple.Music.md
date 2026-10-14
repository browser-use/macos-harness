## Music

### Search

To search for music, click the Search row in the sidebar, then use `set_value` on the search text field. The search starts by itself. If no results show, run `get_app_state` again and see whether they have loaded.

If the search field is not there, scroll the sidebar to the very top. The `filterField` (a search field that appears after a click on `filterBtn`) only filters the view that is open. It does not search the library or the catalog.

To find a playlist, search (with "Your Library" selected in the results) or scroll down in the sidebar.

### Moving around

Scroll an element with its `Scroll Up` or `Scroll Down` action, through `perform_secondary_action`. To scroll further, run the action several times in one `&&` chain.

After you select a sidebar item you may land one level deeper than you expected. `backBtn` takes you back to the top level.

### Playback

Double-click a track to play it: `click` with `--click_count 2`.

Queue a track ("Playing Next") through its "More" button: choose "Play Next" or "Play Last". No "More" button: right-click the track.
