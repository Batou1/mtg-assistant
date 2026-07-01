from app import textutil


def test_splits_on_blank_lines():
    text = "Premier paragraphe.\n\nDeuxième paragraphe.\n\nTroisième paragraphe."
    assert textutil.paragraphs(text) == [
        "Premier paragraphe.", "Deuxième paragraphe.", "Troisième paragraphe.",
    ]


def test_strips_markdown_headings_and_bold():
    text = "# Titre **important** ici"
    assert textutil.paragraphs(text) == ["Titre important ici"]


def test_joins_stray_single_newlines_within_a_paragraph():
    text = "Une phrase\nsur plusieurs lignes\nsans ligne vide."
    assert textutil.paragraphs(text) == ["Une phrase sur plusieurs lignes sans ligne vide."]


def test_regression_crammed_gameplan_from_before_the_prompt_fix():
    # Real output seen in production before the prompt was fixed to emit
    # blank-line-separated paragraphs: everything on one line, with Markdown
    # headings/bold that must not show up as literal characters.
    text = (
        "# Plan de jeu **Stratégie générale :** Ce deck mono-blanc exploite "
        "les synergies autour du gain de vie. **Comment le deck gagne :** La "
        "victoire se construit par attrition."
    )
    result = textutil.paragraphs(text)
    assert len(result) == 1
    assert "#" not in result[0]
    assert "**" not in result[0]


def test_empty_and_none_return_no_paragraphs():
    assert textutil.paragraphs(None) == []
    assert textutil.paragraphs("") == []
    assert textutil.paragraphs("   \n\n   ") == []
