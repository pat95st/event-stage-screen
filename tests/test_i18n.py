import re

from stagescreen.i18n import Msg, set_language, t
from stagescreen.i18n.de import DE
from tests.i18n_extract import all_strings


def test_msg_translation():
    m = Msg("Output {n} does not exist", n=3)
    assert m == "Output 3 does not exist"
    assert t(m, "de") == "Ausgang 3 existiert nicht"
    assert t("unknown text", "de") == "unknown text"
    set_language("de")
    try:
        assert t("Save") == "Speichern"
    finally:
        set_language("en")
    assert t("Save") == "Save"


def test_german_catalog_is_complete():
    missing = sorted(all_strings() - DE.keys())
    assert not missing, "missing German translations:\n" + "\n".join(missing)


def test_catalog_placeholders_match():
    def ph(s):
        return sorted(re.findall(r"\{(\w*)\}", s))

    assert [k for k, v in DE.items() if ph(k) != ph(v)] == []
