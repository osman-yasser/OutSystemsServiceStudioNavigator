from os_module_navigator.automation.sanitize import sanitize_label, sanitize_tree
from os_module_navigator.automation.uia import ElementNode


def test_label_control_types_keep_their_names():
    assert sanitize_label("Button", "Search Modules") == "Search Modules"
    assert sanitize_label("Edit", "Search Applications...") == "Search Applications..."


def test_content_text_is_redacted_by_default():
    assert sanitize_label("Text", "MOD_EX_Secret_Module") == "<redacted len=20>"
    assert sanitize_label("Header", "User Password") == "<redacted len=13>"


def test_known_ui_labels_are_kept_on_text():
    assert sanitize_label("Text", "Recent modules") == "Recent modules"
    assert sanitize_label("Text", "Publish") == "Publish"


def test_tab_items_keep_type_names_but_not_free_text():
    type_name = "ServiceStudio.WebViewImplementation.AggregatorView"
    assert sanitize_label("TabItem", type_name) == type_name
    assert sanitize_label("TabItem", "My Module") == "<redacted len=9>"


def test_icon_glyphs_and_zero_width_characters():
    assert sanitize_label("Text", "") == "<icon>"
    assert sanitize_label("Button", "New​ Application") == "New Application"


def test_secrets_are_scrubbed_even_when_revealing():
    revealed = sanitize_label("Text", "password = hunter2 for a@b.com", reveal=True)
    assert "hunter2" not in revealed
    assert "a@b.com" not in revealed
    assert sanitize_label("Text", "x" * 40, reveal=True) == "<opaque>"


def test_long_labels_are_truncated():
    assert len(sanitize_label("Button", "a b " * 50)) == 80


def test_sanitize_tree_is_recursive_and_does_not_mutate_input():
    tree = ElementNode(
        control_type="Group",
        name="secret",
        children=[ElementNode(control_type="Text", name="nested secret")],
    )
    clean = sanitize_tree(tree)
    assert clean.name == "<redacted len=6>"
    assert clean.children[0].name == "<redacted len=13>"
    assert tree.children[0].name == "nested secret"
