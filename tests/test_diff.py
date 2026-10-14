"""diff.py: the pairing, the three forms of a state read and the line budget."""

from __future__ import annotations

import fakes_c
import pytest
from fakes import El
from fakes_c import HEADER, capture, line, replay

from macos_harness import diff, elements

MAIN = HEADER.format("Main")
SAME = 'Nothing changed in window "Main" since the last read.'


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    return fakes_c.patch(monkeypatch)


def tree(*rows: str):
    """Numbered lines from rows such as `..7 text`: dots for the depth, the number."""
    parts = [fakes_c.ROW.match(row).groups() for row in rows]
    return [line(len(dots), text, id=int(number)) for dots, _, number, text in parts]


def paired(old, new):
    changes = diff.pair(old, new, lambda node: node.id)
    return sorted((c.kind, [node.id for node in c.lines]) for c in changes)


def body(text: str) -> list[str]:
    return text.split("\n")[1:]


# --- chains of reads ------------------------------------------------------------------


@pytest.mark.parametrize("name", fakes_c.names())
def test_chain(name):
    replay(name)


def test_state_read_returns_three_forms_full_tree_diff():
    full, changed, same = replay("three-forms")
    assert full.startswith('Window: "Main", App: App.\n0 ')
    assert changed.startswith(MAIN + "\n~")
    assert same.startswith(SAME)


def test_diff_header_exact():
    assert replay("order")[1].split("\n")[0] == (
        'What changed in window "Main" since the last read: a line that starts with ~ '
        "changed, a line that starts with + is new. Elements that went away are "
        "listed by ID."
    )


def test_removed_elements_summary_line_id_ranges_never_described():
    assert diff.ranges([*range(275, 280), *range(16, 274)]) == "16-273, 275-279"
    assert diff.ranges([44, 23, 24, 44]) == "23-24, 44"
    assert diff.ranges([7]) == "7"
    assert (
        replay("added-subtree")[1].split("\n")[1]
        == "IDs of elements that went away: 3-5"
    )


def test_changed_line_tabs_absolute_depth_complete_new_line():
    assert body(replay("number-reuse")[2])[1] == "~\t\t8 row five"


def test_added_line_tabs_complete_line_whole_subtrees_listed():
    assert body(replay("added-subtree")[1])[1:] == [
        "+\t\t7 list",
        "+\t\t\t8 row 1",
        "+\t\t\t\t9 cell",
        "+\t\t\t10 row 2",
    ]


def test_no_ancestor_context_lines_only_position_hint_most():
    for name in fakes_c.names():
        for text in replay(name):
            if text.startswith("The following"):
                rows = [r for r in body(text) if not r.startswith(("Removed", "The "))]
                assert all(row[0] in "~+" for row in rows)


def test_diff_lines_ordered_new_tree_dfs_position_id():
    marks = [row.split(" ")[0] for row in body(replay("order")[1])]
    assert marks == ["+\t5", "~\t1", "+\t\t6", "~\t4"]


def test_difference_line_budget_overflow():
    texts = replay("budget")
    forms = [text.split(" ")[0] for text in texts]
    assert forms == ["Window:", "What", "Window:", "Window:", "What"]
    old, new = tree("0 a", ".1 b"), tree("2 c", ".3 d")
    changes = diff.pair(old, new, lambda node: node.id)  # 2 added, 1 removed line
    assert diff.text(changes, "Main", "", 2) is None
    assert diff.text(changes, "Main", "", 3).count("\n") == 3


def test_render_line_options_exist_drop_detail_text_ids():
    node = line(2, "list", id=7, detail="(showing 1-5 of 9 items)")
    assert diff.line(node) == "\t\t7 list (showing 1-5 of 9 items)"
    assert diff.line(line(0, "", id=3)) == "3 "
    changes = diff.pair([], [node], lambda n: n.id)
    assert diff.text(changes, "Main", "", 1) == f"{MAIN}\n+{diff.line(node)}"


def test_old_new_renders_paired_structurally_identity():
    old = tree("0 w", ".1 a", ".2 b", ".3 c", "..4 d")
    # the same order: the common start pairs every line, one text differs
    assert paired(old, tree("0 w", ".1 a", ".2 b!", ".3 c", "..4 d")) == [
        *[("none", [n]) for n in (0, 1, 3, 4)],
        ("update", [2]),
    ]
    # 3 moved to the front: it is an insert with its subtree, and nothing is removed
    assert paired(old, tree("0 w", ".3 c", "..4 d", ".1 a", ".2 b")) == [
        ("insert", [3, 4]),
        *[("none", [n]) for n in (0, 1, 2)],
    ]
    # 4 moved to another parent: never paired, so removed there and added here
    assert paired(old, tree("0 w", ".1 a", "..4 d", ".2 b", ".3 c")) == [
        ("insert", [4]),
        *[("none", [n]) for n in (0, 1, 2, 3)],
        ("remove", [4]),
    ]
    # a removed line takes its subtree with it, in one change
    assert ("remove", [3, 4]) in paired(old, tree("0 w", ".1 a", ".2 b"))
    # children are compared only under a paired parent
    assert paired(old, tree("9 w", ".1 a")) == [
        ("insert", [9, 1]),
        ("remove", [*range(5)]),
    ]


@pytest.mark.parametrize(
    "offsets, kept",
    [
        ([], set()),
        ([0, 1, 2], {0, 1, 2}),
        ([2, 0, 1], {1, 2}),
        ([1, 2, 0], {0, 1}),
        ([3, 0, 4, 1, 2], {1, 3, 4}),
    ],
)
def test_moved_siblings_keep_the_longest_run_in_order(offsets, kept):
    assert diff._increasing(offsets) == kept


def test_no_change_form_exact_optional_trailer_next_line():
    assert replay("menu")[3] == (
        'Nothing changed in window "Fixture" since the last read.'
    )
    assert replay("three-forms")[2] == (
        f"{SAME}\nThe focused UI element is 2 text field (settable) Value: two"
    )
    element, revisions = El(), elements.Revisions()
    revisions.present(capture([line(0, "list", element, detail="1 of 9")]), False)
    again = capture([line(0, "list", element, detail="2 of 9")])
    assert revisions.present(again, False) == SAME  # the detail is not compared


def test_first_observation_app_returns_full_tree():
    assert replay("three-forms")[0].split("\n")[1] == "0 standard window Main"


def test_call_flag_forces_full_tree():
    assert replay("base-advances")[4].split("\n")[1:5] == [
        "0 window",
        "\t1 text new",
        "\t2 text 2",
        "\t3 button",
    ]


def test_rebinding_app_always_returns_full_tree():
    state, _, want = fakes_c.chain("three-forms")[0]
    assert elements.Revisions().present(state, False) == want
    assert elements.Revisions().present(state, False) == want  # a new session


def test_menu_mode_tree_state_right_after_full_trees():
    texts = replay("menu")
    assert [text.split("\n")[1][:2] for text in texts[:3]] == ["0 ", "1 ", "1 "]


def test_changed_window_title_alone_does_force_full_tree():
    assert replay("new-window")[2] == SAME.replace("Main", "Two renamed")


def test_revisions_tracked_app_tree_replaced_window_id_new():
    changed = replay("new-window")[1].split("\n")
    assert changed[0] == HEADER.format("Two")
    assert changed[1:3] == [
        "IDs of elements that went away: 0-1",
        "+4 standard window Two",
    ]


def test_diff_base_advances_every_call_whether_model_read():
    texts = replay("base-advances")
    assert texts[2] == SAME  # compared with the second read, which nobody may have read
    assert body(texts[5]) == ["~\t2 text 3"]  # compared with the full tree before it


def test_previous_error_may_reset_diff_base_full_tree():
    first, second, _ = fakes_c.chain("three-forms")
    revisions = elements.Revisions()
    revisions.present(first[0], False)
    # a call that failed presented nothing: the next read is still a diff
    assert revisions.present(second[0], False) == second[2].replace("<HEADER>", MAIN)
    revisions.reset()  # the turn ended
    assert revisions.present(second[0], False).startswith('Window: "Main"')
    assert second[0].lines[0].id == 0


def test_revisions_form_lineage_root_base_cumulative_diff_initial():
    revisions = elements.Revisions()
    states = [state for state, _, _ in fakes_c.chain("base-advances")[:3]]
    texts = [revisions.present(state, False) for state in states]
    assert texts[2] == SAME  # against the first read it would show a changed line
    assert revisions.last is states[2]  # and only the last state is kept


def test_diff_lines_counted_before_they_rendered_removed_ids():
    changes = diff.pair(tree("0 a", ".1 b"), tree("2 c", ".3 d"), id)
    for change in changes:
        change.lines[0].depth = None  # a line that cannot be written
    assert diff.text(changes, "Main", "", 2) is None
    with pytest.raises(TypeError):
        diff.text(changes, "Main", "", 3)


def test_screenshot_only_call_still_does_full_state_read():
    states = [state for state, _, _ in fakes_c.chain("base-advances")[:3]]
    revisions = elements.Revisions()
    revisions.present(states[0], False)
    revisions.present(states[1], False)  # the caller printed only the screenshot line
    assert revisions.present(states[2], False) == SAME


# --- left out on purpose ------------------------------------------------------------------


def every_text() -> str:
    return "\n".join(text for name in fakes_c.names() for text in replay(name))


def test_no_header_variant_for_removed_elements():
    assert "and -" not in every_text()
    assert not [row for row in every_text().split("\n") if row.startswith("-")]


def test_no_master_gate_for_diffing():
    assert replay("three-forms")[1].startswith(MAIN)  # on with no switch anywhere
    assert not hasattr(elements.Revisions(), "enabled")


def test_no_cumulative_diff_variant():
    assert "cumulative" not in every_text()


def test_no_parser_from_text_back_to_a_tree():
    assert not hasattr(diff, "parse") and not hasattr(elements, "parse")


def test_no_debug_payload_on_a_state_result():
    state = fakes_c.chain("three-forms")[0][0]
    assert isinstance(elements.Revisions().present(state, False), str)
