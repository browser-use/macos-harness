"""elements.py: the numbers of a state, and the way from a number to a live element."""

from __future__ import annotations

import fakes_c
import pytest
from fakes import El
from fakes_c import capture, chain, line, replay

from macos_harness import elements, errors
from macos_harness.model import AppTarget

TARGET = AppTarget("/Applications/App.app", "com.example.app", "App", 100)
RETRY = "Read the app state again, then retry with the new element IDs."
PREFIX = "macOS Harness error 100: "
WINDOW = El()


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    ax = fakes_c.patch(monkeypatch)
    ax.apps[100] = El("AXApplication")
    return ax


@pytest.fixture
def revisions():
    """A session that watches its app, as after the first state read."""
    session = elements.Revisions()
    session.watch(TARGET)
    return session


def field(element=None, title="Name", value="", role="AXTextField"):
    key = (role, "", title, "", "", "", "", "")
    return line(1, f"text field {title}", element, role=role, match=key, value=value)


def state(*lines):
    return capture([line(0, "window", WINDOW), *lines])


def reads(*lines):
    return lambda: state(*lines)


def never():
    raise AssertionError("the tree was read again")


def numbers(name: str) -> list[dict[El, int]]:
    """The number of every element after each read of a chain."""
    session, out = elements.Revisions(), []
    for read, full, _ in chain(name):
        session.present(read, full)
        out.append({node.element: node.id for node in read.lines})
    return out


def failure(call, *arguments, **options) -> errors.HarnessError:
    with pytest.raises(errors.HarnessError) as caught:
        call(*arguments, **options)
    return caught.value


# --- numbering --------------------------------------------------------------------------


def test_ids_assigned_depth_first_pre_order_strictly_1():
    assert list(numbers("added-subtree")[0].values()) == list(range(7))


def test_numbering_base_0_but_flips_1_some_trees():
    first, sheet, *_ = numbers("nothing-inherits")
    assert list(first.values()) == [0, 1]
    assert list(sheet.values()) == [1, 2]  # appended, and nothing inherited


def test_full_tree_renumbers_everything():
    texts = replay("nothing-inherits")
    assert texts[1].split("\n")[1] == "1 sheet"  # full by the budget: numbers stay
    assert texts[2].endswith("~\t2 button Saved")  # and the next read is a diff
    assert texts[3].split("\n")[1] == "0 sheet"  # full by the flag: from 0 again


def test_inside_diff_chain_unlisted_element_keeps_number():
    before, after = numbers("order")
    assert all(after[element] == number for element, number in before.items())


def test_new_elements_diff_mostly_get_ids_above_previous():
    first, second, third = (list(read.values()) for read in numbers("number-reuse"))
    assert first == [0, 1, 2, 3, 4, 5, 6]
    assert second == [0, 7, 8, 9, 4, 5, 6]  # after the largest, the gap 1-3 stays
    assert third == [0, 7, 8, 4, 9, 5, 6]  # 8 and 9 left the tree and are used again


def test_inside_added_subtree_ids_next_free_numbers_dfs():
    assert list(numbers("added-subtree")[1].values())[3:7] == [7, 8, 9, 10]


def test_where_new_subtree_s_first_id_lands_rule():
    texts = replay("focus-outside")  # a focus outside the tree counts after the tree
    assert texts[0].endswith("is 2 text field Search")
    assert texts[1].endswith("is 3 text field Search")
    button, twice, outside = El(), El(), El()
    lines = [line(0, "window"), line(1, "button", button)]
    session = elements.Revisions()
    session.present(capture(lines), False)
    read = capture(
        [*lines, line(1, "row", twice), line(1, "row", twice)],
        focused=line(1, "button", button),
        selected=[line(1, "row", twice), line(0, "text", outside)],
    )
    session.present(read, False)
    assert [node.id for node in read.lines] == [0, 1, 2, 2]  # one element, one number
    assert read.focused.id == 1
    assert [node.id for node in read.selected] == [2, 3]


def test_key_elements_element_object_cfequal(fake, monkeypatch):
    class OneHash(El):  # every element in one hash bucket
        def __hash__(self):
            return 7

    first, second = OneHash(), OneHash()
    session = elements.Revisions()
    session.present(state(line(1, "a", first), line(1, "b", second)), False)
    read = state(line(1, "b", second), line(1, "c", OneHash()))
    session.present(read, False)
    assert [node.id for node in read.lines[1:]] == [2, 3]

    # the app hands out a new object for the same element on every read
    monkeypatch.setattr(fake, "same", lambda a, b: a.attrs == b.attrs)
    again = state(line(1, "b", OneHash(attrs={"uid": 1})))
    session.present(state(line(1, "b", OneHash(attrs={"uid": 1}))), True)
    assert session.present(again, False).startswith("Nothing changed in window")


def test_stable_ids_need_long_lived():
    kept = numbers("order")[1]
    read = chain("order")[1][0]
    elements.Revisions().present(read, False)  # a new process knows no numbers
    assert [node.id for node in read.lines] == list(range(7))
    assert list(kept.values()) == [0, 5, 1, 2, 6, 3, 4]


def test_axidentifier_axdomidentifier_hints_show_keys():
    button, session = El(), elements.Revisions()
    text = line(1, "text")
    session.present(state(line(1, "button OK, ID: ok", button), text), False)
    renamed = state(line(1, "button OK, ID: confirm", button), text)
    assert session.present(renamed, False).endswith("~\t1 button OK, ID: confirm")
    other = state(line(1, "button OK, ID: confirm"), text)  # the ID on a new element
    assert "IDs of elements that went away: 1\n+	3 button" in session.present(
        other, False
    )


# --- from a number to an element ----------------------------------------------------


def test_stale_element_refetched_before_acting_trusted(revisions):
    old, new = El(), El()
    revisions.present(state(field(old)), False)
    assert revisions.resolve(1, never).element is old  # alive: used as stored
    old.dead = True
    assert revisions.resolve(1, reads(field(new))).element is new
    assert revisions.resolve(1, never).element is new  # the line holds the new one


def test_prepare_step_before_any_element_action_returns_refetchable(revisions):
    read = state(field(), line(1, "button"))
    revisions.present(read, False)
    assert revisions.resolve(2, never) is read.lines[2]
    outside = line(1, "text field Search")
    revisions.present(capture(read.lines, focused=outside), False)
    assert revisions.resolve(3, never) is outside  # a number of the focus line works


def test_refetch_has_six_distinct_failure_outcomes(revisions):
    # the six: unknown number, no monitor (its own test below), several alike before
    # the new read, none after it, several after it. Dead with and without a new read
    # is one case here: a dead element is always looked for again.
    one, two, three = El(dead=True), El(dead=True), El(dead=True)
    revisions.present(state(field(one), field(two), field(three, "Other")), False)
    refetch = (
        "The element went away, and more than one element now fits its description, so "
        f"looking it up again could not be {{}}. {RETRY}"
    )
    outcomes = {
        (
            9999,
            never,
        ): "No element has the ID 9999 in the last read. Use an ID from the newest `get_app_state` text.",
        (1, never): refetch.format("started"),  # its twin is in the old tree
        (3, reads(field(), field())): f"That element ID is from an older read. {RETRY}",
        (3, reads(field(title="Other"), field(title="Other"))): refetch.format(
            "finished"
        ),
    }
    for (number, reread), text in outcomes.items():
        assert failure(revisions.resolve, number, reread).message == (PREFIX + text)


def test_refetch_can_ignore_changed_value_uses_role_frequency(revisions):
    old, new = El(dead=True), El()
    revisions.present(state(field(old, value="draft")), False)
    typed = reads(field(new, value="draft!"))
    assert "from an older read" in failure(revisions.resolve, 1, typed).message
    assert revisions.resolve(1, typed, ignore_value=True).element is new


def test_a_dead_element_is_found_again_only_when_its_role_is_unique(revisions):
    old, new = El(dead=True), El()
    revisions.present(state(field(old)), False)
    mixed = reads(field(new), field(role="AXComboBox"))
    assert revisions.resolve(1, mixed).element is new  # one of its role: unique
    new.dead = True
    twins = reads(field(), field())
    assert "finished" in failure(revisions.resolve, 1, twins).message


def test_fourth_refetch_error_sentence_exists():
    session = elements.Revisions()  # nobody called watch
    session.present(state(field()), False)
    assert failure(session.resolve, 1, never).message == PREFIX + (
        "Internal error: this app session has no change monitor."
    )
    assert failure(session.resolve, 7, never).message.count("No element has the ID")


def test_invalid_element_id_machine_readable_error_tag_next(revisions):
    revisions.present(state(field(El(dead=True)), field(El(dead=True))), False)
    assert failure(revisions.resolve, 5, never).tag == "invalid_element_id"
    assert failure(revisions.resolve, 1, never).tag == "invalid_element_id"
    revisions.present(state(field(El(dead=True))), False)
    gone = failure(revisions.resolve, 1, reads())
    assert (gone.tag, gone.code) == ("invalid_element_id", 100)


def test_element_tree_caches_invalidation_monitor_fed_ax_notifications(fake, revisions):
    (observer,) = fake.observers
    assert observer.names == ["AXUIElementDestroyed"]
    old, new = El(), El()
    revisions.present(state(field(old)), False)
    observer.fire("AXUIElementDestroyed", old)  # the app says so; the element answers
    assert revisions.resolve(1, reads(field(new))).element is new
    observer.fire("AXUIElementDestroyed", new)
    revisions.present(state(field(new)), False)  # a new read forgets the marks
    assert revisions.resolve(1, never).element is new


def test_watch_once_per_app_and_reset_forgets(fake, revisions):
    revisions.watch(TARGET)
    assert len(fake.observers) == 1
    fake.apps[200] = El("AXApplication")
    revisions.watch(AppTarget(TARGET.path, TARGET.bundle_id, "App", 200))  # relaunched
    assert [observer.stopped for observer in fake.observers] == [True, False]
    revisions.present(state(field()), False)
    revisions.reset()
    assert fake.observers[1].stopped and revisions.last is None
    assert failure(revisions.resolve, 1, never).message.count("No element has the ID")
