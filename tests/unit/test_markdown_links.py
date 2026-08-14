"""Structural Markdown link extraction, including false-positive controls."""

from ferumind.core.markdown_links import MarkdownLink, extract_markdown_links


def test_inline_links_images_and_multiline_links_keep_source_order() -> None:
    markdown = """\
[one](one.md)
![image](image.png)
[multi](
  nested/two.md
)
"""

    assert extract_markdown_links(markdown) == [
        MarkdownLink("one.md", 1, "link"),
        MarkdownLink("image.png", 2, "image"),
        MarkdownLink("nested/two.md", 3, "link"),
    ]


def test_fenced_and_inline_code_links_are_suppressed() -> None:
    markdown = """\
`[inline](no.md)` and [yes](yes.md)
```markdown
[fenced](no.md)
```
~~~
![also fenced](no.png)
~~~
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("yes.md", 1, "link")]


def test_fences_inside_list_and_quote_containers_are_suppressed() -> None:
    markdown = """\
- ```markdown
  [list code](no.md)
  ```
> - ~~~
>   ![nested code](no.png)
>   ~~~
[yes](yes.md)
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("yes.md", 7, "link")]


def test_unclosed_list_fence_ends_when_content_escapes_container() -> None:
    markdown = """\
- ```markdown
  [list code](no.md)
[yes](yes.md)
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("yes.md", 3, "link")]


def test_indented_code_blocks_are_suppressed_in_top_level_quote_and_list() -> None:
    markdown = """\
    [top-level code](no.md)
>     [quoted code](no.md)
-     [list code](no.md)
[yes](yes.md)
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("yes.md", 4, "link")]


def test_indented_lazy_paragraph_continuation_remains_visible() -> None:
    markdown = """\
paragraph
    [continued link](continued.md)
> paragraph
>     [continued quote link](quote.md)
- paragraph
      [continued list link](list.md)
"""

    assert extract_markdown_links(markdown) == [
        MarkdownLink("continued.md", 2, "link"),
        MarkdownLink("quote.md", 4, "link"),
        MarkdownLink("list.md", 6, "link"),
    ]


def test_reference_links_resolve_full_collapsed_shortcut_and_images() -> None:
    markdown = """\
[full][target]
[collapsed][]
[shortcut]
![image][asset]

[target]: docs/full.md
[collapsed]: docs/collapsed.md
[shortcut]: docs/shortcut.md
[asset]: <images/photo.png>
"""

    assert extract_markdown_links(markdown) == [
        MarkdownLink("docs/full.md", 1, "link"),
        MarkdownLink("docs/collapsed.md", 2, "link"),
        MarkdownLink("docs/shortcut.md", 3, "link"),
        MarkdownLink("images/photo.png", 4, "image"),
    ]


def test_linked_image_reports_both_document_and_asset_in_opener_order() -> None:
    markdown = "[![diagram](images/diagram.png)](docs/explanation.md)\n"

    assert extract_markdown_links(markdown) == [
        MarkdownLink("docs/explanation.md", 1, "link"),
        MarkdownLink("images/diagram.png", 1, "image"),
    ]


def test_nested_link_invalidates_outer_link_but_nested_image_does_not() -> None:
    markdown = """\
[outer [inner](inner.md)](not-a-link.md)
[outer ![image](image.png)](outer.md)
"""

    assert extract_markdown_links(markdown) == [
        MarkdownLink("inner.md", 1, "link"),
        MarkdownLink("outer.md", 2, "link"),
        MarkdownLink("image.png", 2, "image"),
    ]


def test_reference_labels_are_casefolded_and_whitespace_collapsed() -> None:
    markdown = "[use][A  Label]\n\n[a label]: target.md\n"

    assert extract_markdown_links(markdown) == [MarkdownLink("target.md", 1, "link")]


def test_duplicate_reference_definitions_are_not_link_uses() -> None:
    markdown = """\
[use][target]

[target]: first.md
[target]: ignored.md
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("first.md", 1, "link")]


def test_uri_and_email_autolinks_are_links_but_raw_url_is_not() -> None:
    markdown = "<https://example.test/a> <person@example.test> https://example.test/raw\n"

    assert extract_markdown_links(markdown) == [
        MarkdownLink("https://example.test/a", 1, "link"),
        MarkdownLink("mailto:person@example.test", 1, "link"),
    ]


def test_escapes_and_entities_are_decoded_without_percent_decoding() -> None:
    markdown = r"[escaped](a\(b\).md) [entity](a&amp;b%20c.md)" + "\n"

    assert extract_markdown_links(markdown) == [
        MarkdownLink("a(b).md", 1, "link"),
        MarkdownLink("a&b%20c.md", 1, "link"),
    ]


def test_escaped_open_bracket_is_not_a_link() -> None:
    assert extract_markdown_links(r"\[not](no.md) [yes](yes.md)") == [
        MarkdownLink("yes.md", 1, "link")
    ]


def test_escaped_image_marker_leaves_an_ordinary_link() -> None:
    markdown = r"\![link](linked.md) \\![image](image.png)"

    assert extract_markdown_links(markdown) == [
        MarkdownLink("linked.md", 1, "link"),
        MarkdownLink("image.png", 1, "image"),
    ]


def test_escaped_backticks_do_not_suppress_links() -> None:
    assert extract_markdown_links(r"\`[linked](linked.md)\`") == [
        MarkdownLink("linked.md", 1, "link")
    ]


def test_malformed_destination_or_title_is_not_a_link() -> None:
    markdown = """\
[bad title](missing.md not-a-title)
[bad angle](<missing.md>suffix)
[good title](exists.md "A title")

[reference]
[reference]: missing.md not-a-title
"""

    assert extract_markdown_links(markdown) == [MarkdownLink("exists.md", 3, "link")]


def test_link_cannot_cross_a_blank_line() -> None:
    assert extract_markdown_links("[not a\n\nlink](missing.md)\n") == []
    assert extract_markdown_links("[not a link](\n\nmissing.md\n)\n") == []


def test_backtick_fence_info_string_with_backtick_does_not_open_a_fence() -> None:
    markdown = "``` invalid`info\n[linked](linked.md)\n"

    assert extract_markdown_links(markdown) == [MarkdownLink("linked.md", 2, "link")]


def test_empty_inline_destination_names_the_current_document() -> None:
    assert extract_markdown_links("[self]()\n") == [MarkdownLink("", 1, "link")]
