import pytest

from opsdigest import media


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_document_and_sidecar_newlines(tmp_path, newline):
    log = tmp_path / "sample.log"
    log.write_bytes(newline.join(["ERROR", "", "ok"]).encode())
    assert media.extract_text(log) == "ERROR\nok"

    img = tmp_path / "sample.jpg"
    text = newline.join([
        "# Sample", "", "- sha256: abc", "",
        "## Описание", "", "Details", "",
        "## Текст на изображении", "", "ERR-1", "",
    ])
    media.sidecar_path(img).write_bytes(text.encode("utf-8"))
    desc = media.read_sidecar(img, "abc")
    assert desc["details"] == "Details"
    assert desc["text"] == "ERR-1"
    assert media.read_sidecar(img, "wrong") is None
