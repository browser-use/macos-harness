## Spotify

### Playback

After a click that starts playback, Spotify shows the old state for a moment: the track may still look paused, or the old track may still be shown. Do not act again. First run `get_app_state` once more and see whether the click worked. Do not sleep: the state is up to date by the time you read again.

### Search

Check that the search field has focus before you press `Return`. With the focus somewhere else, `Return` can change what is playing.

### Moving around

Much of what Spotify shows comes from the network, so an answer can take a moment. A search can say "no results" for an instant. Run `get_app_state` again before you change your plan.

Note: a link taken from the Spotify app works only after it is rewritten as a normal web link: `open.spotify.com` in place of `xpui.app.spotify.com`. Use only links that stand word for word in the state above. An ID is valid only with its own type: the ID of an "album" URL cannot be put into a "track" URL.
