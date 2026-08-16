"""Unit tests for overlap-diff merging."""
from src.merge import (apply_disputes, collapse_adjacent_repeats,
                       common_prefix_len, diff_plan, ensure_period,
                       find_disputed_blocks, merge_segments,
                       strip_trailing_repeat, union_text)


def test_merge_appends_new_tail():
    committed, appended = merge_segments(["hello", "world"], "world today is sunny")
    assert committed == ["hello", "world", "today", "is", "sunny"]
    assert appended == "today is sunny"


def test_merge_no_overlap_prepends():
    committed, appended = merge_segments(["alpha"], "beta gamma")
    assert committed == ["alpha", "beta", "gamma"]
    assert appended == "beta gamma"


def test_merge_empty_new():
    committed, appended = merge_segments(["a"], "")
    assert committed == ["a"]
    assert appended == ""


def test_merge_empty_committed():
    committed, appended = merge_segments([], "first words here")
    assert committed == ["first", "words", "here"]
    assert appended == "first words here"


def test_merge_fuzzy_overlap():
    committed = ["the", "quick", "brown", "fox"]
    new = "brown fox jumped over the lazy dog"
    merged, appended = merge_segments(committed, new)
    assert appended.startswith("jumped")
    assert merged == ["the", "quick", "brown", "fox", "jumped", "over", "the", "lazy", "dog"]


def test_ensure_period():
    assert ensure_period(["hello"]) == ["hello", "."]
    assert ensure_period(["hello"]) == ensure_period(["hello", "."])
    assert ensure_period(["ok!"]) == ["ok!"]
    assert ensure_period(["ok"]) == ["ok", "."]


def test_common_prefix_len():
    assert common_prefix_len("hello world", "hello there") == 6
    assert common_prefix_len("abc", "xyz") == 0


def test_diff_plan():
    delete, typed = diff_plan("hello world foo", "hello world bar")
    assert delete == 3
    assert typed == "bar"
    delete, typed = diff_plan("same", "same")
    assert delete == 0 and typed == ""


def test_union_text_returns_longer_when_subsequence():
    a = "hello world this is a test goodbye world"
    b = "hello world goodbye world"
    assert union_text(a, b) == a
    assert union_text(b, a) == a


def test_union_text_keeps_unique_words():
    a = "hello world goodbye"
    b = "hello world this is a test goodbye"
    assert union_text(a, b) == b


def test_union_text_lcs_merge():
    a = "the quick brown fox"
    b = "the quick lazy dog jumps"
    out = union_text(a, b)
    for w in "quick brown fox lazy dog jumps".split():
        assert w in out.split()


def test_union_text_empty():
    assert union_text("", "hello") == "hello"
    assert union_text("hello", "") == "hello"
    assert union_text("", "") == ""


def test_union_text_rejects_single_word_noise():
    # A one-word disagreement between passes is transcription noise, not a
    # missing sentence — it must NOT be inserted into the primary.
    a = "the majority have suffered"
    b = "the majority is have suffered"
    assert union_text(a, b) == a


def test_union_text_inserts_dropped_sentence():
    # Secondary contains a whole sentence the primary missed; it must be
    # recovered even though it sits between matching context.
    primary = "hello world goodbye"
    secondary = "hello world various versions have evolved over the years goodbye"
    out = union_text(primary, secondary)
    assert "various versions have evolved over the years" in out
    assert out.startswith("hello world") and out.endswith("goodbye")


def test_merge_segments_normalized_overlap():
    # "years." vs "years" must still deduplicate (punctuation-aware overlap).
    committed, appended = merge_segments(["over", "the", "years."],
                                         "years, sometimes by accident")
    assert appended == "sometimes by accident"
    assert committed == ["over", "the", "years.", "sometimes", "by", "accident"]


def test_merge_segments_max_overlap_cap():
    committed = ["e", "f", "g", "h", "i", "j"]
    new = "e f g h i j k"
    # Cap of 3: a 6-word run (longer than any real audio overlap) can't be
    # claimed as overlap, so the whole new text is appended — nothing eaten.
    merged, appended = merge_segments(committed, new, max_overlap=3)
    assert appended == "e f g h i j k"
    assert len(merged) == len(committed) + 7
    # Without a cap the run is claimed and 'k' is the only new tail.
    merged2, appended2 = merge_segments(committed, new)
    assert appended2 == "k"


def test_disputes_none_when_identical():
    assert find_disputed_blocks("the quick brown fox", "the quick brown fox") == []


def test_disputes_ignore_single_word_diff():
    # One-word differences (homophones) are the correcting pass's job.
    assert find_disputed_blocks(
        "build a basic login system",
        "build a basic logging system") == []


def test_disputes_detect_semantic_substitution():
    primary = "If we lose the database the four web applications would be gone"
    verify = "If we lose the database the whole application is basically gone"
    disputes = find_disputed_blocks(primary, verify)
    assert len(disputes) == 1
    d = disputes[0]
    assert d.primary_text == "four web applications would be"
    assert d.verify_text == "whole application is basically"
    assert d.prefix == "If we lose the database the "
    assert d.suffix == " gone"
    assert d.index == 0


def test_disputes_merge_short_shared_words():
    # "on the back end" vs "in the list" shares "the"; the whole substitution
    # must stay one dispute block instead of fragmenting into 1-word diffs.
    primary = "the customer should immediately appear on the back end without refreshing"
    verify = "the customer should immediately appear in the list without refreshing"
    disputes = find_disputed_blocks(primary, verify)
    assert len(disputes) == 1
    d = disputes[0]
    assert d.prefix == "the customer should immediately appear "
    assert d.suffix == " without refreshing"
    assert "on the back end" in d.primary_text
    assert "in the list" in d.verify_text


def test_disputes_low_conf_flags():
    primary = "we lost the business strategy team"
    verify = "we lost the business logic module"
    disputes = find_disputed_blocks(
        primary, verify, primary_low={"business", "strategy"})
    assert len(disputes) == 1
    assert disputes[0].primary_low_conf is True
    assert disputes[0].verify_low_conf is False


def test_disputes_multiple_blocks_and_cap():
    primary = "a w x b c d y z"
    verify = "a p q b c d r s"
    disputes = find_disputed_blocks(primary, verify)
    assert len(disputes) == 2
    assert [d.primary_text for d in disputes] == ["w x", "y z"]
    capped = find_disputed_blocks(primary, verify, max_blocks=1)
    assert len(capped) == 1


def test_apply_disputes_keeps_primary_by_default():
    primary = "the four web applications would be gone now"
    verify = "the whole application is basically gone now"
    disputes = find_disputed_blocks(primary, verify)
    assert apply_disputes(primary, disputes, {}) == primary


def test_apply_disputes_splices_verify_choice():
    primary = "If we lose the database the four web applications would be gone"
    verify = "If we lose the database the whole application is basically gone"
    disputes = find_disputed_blocks(primary, verify)
    out = apply_disputes(primary, disputes, {0: "B"})
    assert out == "If we lose the database the whole application is basically gone"
    # choosing A (or anything non-B) keeps the primary wording
    assert apply_disputes(primary, disputes, {0: "A"}) == primary
    assert apply_disputes(primary, disputes, {0: "C"}) == primary


def test_apply_disputes_no_disputes_is_identity():
    assert apply_disputes("hello world", [], {}) == "hello world"


# ----------------------------------------------------------- echo / repeat de-dup


def test_strip_trailing_repeat_removes_non_adjacent_echo():
    # "we go to the store" repeated at the end (echo) is dropped; the real
    # earlier content stays.
    assert strip_trailing_repeat(
        "we go to the store every day we go to the store"
    ) == "we go to the store every day"


def test_strip_trailing_repeat_punctuation_insensitive():
    # "over the years," vs "over the years." still counts as a repeat.
    assert strip_trailing_repeat("over the years, over the years.") == "over the years,"


def test_strip_trailing_repeat_preserves_short_text():
    # Below MIN_REPEAT_WORDS a trailing repetition is a real stutter.
    assert strip_trailing_repeat("no no no") == "no no no"
    assert strip_trailing_repeat("hello world") == "hello world"
    assert strip_trailing_repeat("") == ""


def test_collapse_adjacent_repeats_collapses_echo():
    assert collapse_adjacent_repeats("go to the store go to the store") == "go to the store"
    assert collapse_adjacent_repeats(
        "thank you so much thank you so much thank you so much"
    ) == "thank you so much"
    assert collapse_adjacent_repeats(
        "alpha beta gamma delta alpha beta gamma delta"
    ) == "alpha beta gamma delta"


def test_collapse_adjacent_repeats_punctuation_insensitive():
    assert collapse_adjacent_repeats(
        "The plan worked. The plan worked. The plan worked."
    ) == "The plan worked."


def test_collapse_adjacent_repeats_preserves_stutters():
    # 1-2 word repeats are normal speech, never collapsed.
    assert collapse_adjacent_repeats("no no no") == "no no no"
    assert collapse_adjacent_repeats("the end the end") == "the end the end"
    assert collapse_adjacent_repeats("right right right now") == "right right right now"


def test_collapse_adjacent_repeats_unchanged_unique_text():
    text = "the quick brown fox jumps over the lazy dog"
    assert collapse_adjacent_repeats(text) == text