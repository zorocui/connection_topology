from pathlib import Path

DEVICE_LIST_JS = Path("app/static/js/device-list.js")
APP_CSS = Path("app/static/css/app.css")


def script_text() -> str:
    return DEVICE_LIST_JS.read_text(encoding="utf-8")


def test_mobile_shell_does_not_expand_to_navigation_minimum_width():
    stylesheet = APP_CSS.read_text(encoding="utf-8")

    assert ".shell { grid-template-columns: minmax(0, 1fr); }" in stylesheet
    assert ".sidebar { min-width: 0;" in stylesheet
    assert ".sidebar nav { min-width: 0; width: 100%;" in stylesheet


def test_page_size_change_preserves_query_and_resets_page():
    script = script_text()

    assert 'getElementById("device-page-size")' in script
    assert 'url.searchParams.set("page_size", pageSize.value)' in script
    assert 'url.searchParams.set("page", "1")' in script
    assert "window.location.assign(url)" in script


def test_search_input_submits_form_on_enter():
    script = script_text()

    assert 'getElementById("device-search-form")' in script
    assert 'getElementById("device-search")' in script
    assert "searchForm?.requestSubmit()" in script


def test_direct_page_jump_clamps_and_supports_enter():
    script = script_text()

    assert 'getElementById("device-page-jump-input")' in script
    assert 'getElementById("device-page-jump-button")' in script
    assert "Math.min(Math.max(requestedPage, 1), totalPages)" in script
    assert 'event.key !== "Enter"' in script
    assert 'toast("请输入有效页码", "error")' in script
