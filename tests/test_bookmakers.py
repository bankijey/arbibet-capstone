from arbibet_capstone.bookmakers import to_parser_name


def test_msport_is_renamed_to_the_name_the_parser_registry_uses() -> None:
    assert to_parser_name("msport") == "msports"


def test_books_whose_names_already_agree_pass_through_untouched() -> None:
    for book in ("sportybet", "bet9ja", "livescorebet", "ilotbet"):
        assert to_parser_name(book) == book


def test_an_unknown_book_passes_through_rather_than_raising() -> None:
    # Bronze holds 14 books; nine have no parser yet. Deciding what to do about
    # them belongs to the caller, so this function does not editorialise.
    assert to_parser_name("betking") == "betking"
