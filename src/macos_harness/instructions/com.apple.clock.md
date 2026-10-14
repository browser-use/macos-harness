## Clock

What you can do for the person in Clock:

- World Clock: add a city or remove one.
- Timer: start, pause, resume, cancel.
- Stopwatch: start, stop, lap, reset.
- Add, edit, remove, switch on or switch off alarms.

### Add a city to the World Clock

1. Click the toolbar tab "World Clock" unless it is open already.
2. Find the clocks that exist now: look at the ID of each container that has exactly one text child. Ignore the places listed under the heading "world map". That list is not updated when a clock is removed.
3. If the city is missing, click the toolbar menu button with the description "Add a clock".
4. A sheet for searching opens. Search by setting the value of its text field with `set_value`. A click on the text of a city adds it and closes the sheet.

### Start a timer

1. Click the toolbar tab "Timer" ("Timers" in British English) unless it is open already.
2. Find out the state of the timer before anything else.
3. **Stopped**: go on. **Running** or **Paused**: tell the person that a timer exists, and offer to cancel it and start the new one.
4. Split the wanted time into **hours** (0 to 23), **minutes** (0 to 59) and **seconds** (0 to 59). "90 seconds" is 0 hours, 1 minute, 30 seconds. "3600 seconds" is 1 hour, 0 minutes, 0 seconds. Refuse a time above 23:59:59: Clock cannot run such a timer.
5. Find the container with the identifier "TimePicker". Its three slider children are hours, minutes and seconds. Set them in that order.
6. For each of the three sliders:
    1. Click it to give it focus.
    2. Type the number with `type_text`. Never use `set_value` here. At most 2 digits: hours up to 23, minutes and seconds up to 59.
7. Press "Start" (a button, found by its description).

### Start a stopwatch

1. Click the toolbar tab "Stopwatch" unless it is open already.
2. Find out the state of the stopwatch.
3. **Running** (the `StartStopButton` says "Stop"): offer to stop or restart it. **Stopped** (it says "Start"): press the Start button.

### Make an alarm

1. Click the toolbar tab "Alarm" unless it is open already.
2. Click the toolbar menu button with the description "Add an alarm".
3. A sheet opens:
    - Time: use `set_value` on the date time area, in the same format as its current Value.
    - Leave the AM/PM radio button alone. It follows the date time area.
    - Repeat days: seven toggle buttons with the values S, M, T, W, T, F, S, one per day of the week. A toggle that is on means the alarm repeats on that day.
    - There are also elements for the label, the sound and snoozing.
4. Finish with the "Save" button.
